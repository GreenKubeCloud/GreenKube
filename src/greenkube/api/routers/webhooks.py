"""HTTP boundary for authenticated Git-provider webhook deliveries."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from greenkube.api.dependencies import get_gitops_binding_repository
from greenkube.automation.webhooks import (
    MAX_WEBHOOK_BODY_BYTES,
    WebhookErrorCode,
    WebhookProcessingError,
)
from greenkube.core.observability import (
    elapsed,
    record_automation_failure,
    record_pull_request_state,
    record_webhook_delivery,
    start_timer,
)
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderRepository,
    WebhookDelivery,
    WebhookDeliveryRepository,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_SIGNATURE_HEADERS = {
    "github": "x-hub-signature-256",
    "gitea": "x-gitea-signature",
    "gitlab": "x-gitlab-token",
}
_ERROR_STATUS_CODES = {
    WebhookErrorCode.INVALID_BODY: 400,
    WebhookErrorCode.INVALID_HEADERS: 400,
    WebhookErrorCode.INVALID_PAYLOAD: 400,
    WebhookErrorCode.INVALID_PROVIDER: 400,
    WebhookErrorCode.INVALID_PROVIDER_ID: 400,
    WebhookErrorCode.INVALID_SIGNATURE: 401,
    WebhookErrorCode.BODY_TOO_LARGE: 413,
    WebhookErrorCode.SECRET_UNAVAILABLE: 503,
    WebhookErrorCode.UNSUPPORTED_EVENT: 400,
}


class _WebhookLifecycleReconciler(Protocol):
    async def reconcile(self, event: PullRequestEvent, *, newly_deduplicated: bool) -> Any: ...


@router.post("/webhooks/{provider_id}")
async def process_webhook(
    provider_id: str,
    request: Request,
    provider_repository: GitOpsProviderRepository = Depends(get_gitops_binding_repository),
) -> dict[str, Any]:
    """Record bounded webhook status and ingress-to-response lag."""
    timer = start_timer()
    metric_status: str | None = "error"
    try:
        result = await _process_webhook(provider_id, request, provider_repository)
    except asyncio.CancelledError:
        metric_status = None
        raise
    except HTTPException as error:
        if error.status_code in {400, 401, 413}:
            metric_status = "rejected"
        elif error.status_code >= 500:
            metric_status = "unavailable"
        raise
    else:
        metric_status = _webhook_metric_status(result.get("status"), result.get("duplicate"))
        return result
    finally:
        if metric_status is not None:
            record_webhook_delivery(metric_status, elapsed(timer))
            if metric_status not in {"received", "processed", "duplicate"}:
                record_automation_failure("webhook")


async def _process_webhook(
    provider_id: str,
    request: Request,
    provider_repository: GitOpsProviderRepository,
) -> dict[str, Any]:
    """Verify and persist one raw delivery using its persisted provider config.

    Provider-native HMAC verification or constant-time shared-token comparison
    is the authentication boundary before a delivery can be recorded.
    """
    service = getattr(request.app.state, "webhook_processing_service", None)
    if service is None or not callable(getattr(service, "process", None)):
        raise HTTPException(status_code=503, detail="webhook_processor_unavailable")

    try:
        provider = await provider_repository.get_provider_config(provider_id)
    except Exception:
        raise HTTPException(status_code=503, detail="provider_configuration_unavailable") from None
    if provider is None or provider.id != provider_id or not provider.enabled:
        raise HTTPException(status_code=503, detail="provider_configuration_unavailable")

    body = await _read_limited_body(request)
    try:
        result = await service.process(
            provider_id=provider_id,
            provider_type=provider.provider_type,
            headers=request.headers,
            body=body,
        )
    except WebhookProcessingError as error:
        status_code = _error_status_code(error, provider.provider_type, request.headers)
        raise HTTPException(status_code=status_code, detail=error.code.value) from None

    event = cast(PullRequestEvent | None, result.event or result.delivery.event)
    if event is None:
        raise HTTPException(status_code=503, detail="webhook_result_unavailable")

    delivery_status = result.delivery.status
    lifecycle_outcome: str | None = None
    lifecycle_reconciler = cast(
        _WebhookLifecycleReconciler | None,
        getattr(request.app.state, "pull_request_lifecycle_reconciler", None),
    )
    duplicate = result.is_duplicate
    claimed = False
    if result.should_process:
        claim = getattr(service, "claim_for_processing", None)
        if not callable(claim):
            raise HTTPException(status_code=503, detail="webhook_processor_unavailable")
        claimed = await cast(Callable[[PullRequestEvent], Awaitable[bool]], claim)(event)
        if not claimed:
            duplicate = True
            delivery_repository = getattr(service, "delivery_repository", None)
            get_delivery = getattr(delivery_repository, "get_webhook_delivery", None)
            current = (
                await cast(Callable[[str, str], Awaitable[WebhookDelivery | None]], get_delivery)(
                    event.provider_id,
                    event.delivery_id,
                )
                if callable(get_delivery)
                else None
            )
            delivery_status = getattr(current, "status", "processing")
            lifecycle_outcome = "processing"

    if claimed and event.action == "closed" and lifecycle_reconciler is None:
        delivery_status = await _update_delivery_status(
            service,
            event.provider_id,
            event.delivery_id,
            status="reconcile_unavailable",
            error_code="lifecycle_unavailable",
            expected_statuses=("processing",),
        )
        raise HTTPException(status_code=503, detail="webhook_lifecycle_unavailable")

    if claimed and lifecycle_reconciler is None:
        delivery_status = await _update_delivery_status(
            service,
            event.provider_id,
            event.delivery_id,
            status="processed",
            processed_at=datetime.now(timezone.utc),
            expected_statuses=("processing",),
        )
        lifecycle_outcome = "ignored"
    elif claimed and lifecycle_reconciler is not None:
        try:
            lifecycle = await lifecycle_reconciler.reconcile(event, newly_deduplicated=True)
        except Exception as error:
            await _update_delivery_status(
                service,
                event.provider_id,
                event.delivery_id,
                status="retryable",
                error_code="lifecycle_reconcile_failed",
                expected_statuses=("processing",),
            )
            logger.warning(
                "Webhook lifecycle reconciliation failed for delivery %s (%s).",
                event.delivery_id,
                type(error).__name__,
            )
            raise HTTPException(status_code=503, detail="webhook_lifecycle_unavailable") from None
        lifecycle_outcome = getattr(getattr(lifecycle, "outcome", None), "value", None)
        if not isinstance(lifecycle_outcome, str):
            lifecycle_outcome = None
        else:
            metric_state = lifecycle_outcome if lifecycle_outcome in {"merged", "closed"} else "unchanged"
            record_pull_request_state(metric_state, provider.provider_type)
        if lifecycle_outcome == "unlinked":
            await _update_delivery_status(
                service,
                event.provider_id,
                event.delivery_id,
                status="retryable",
                error_code="pull_request_unlinked",
                expected_statuses=("processing",),
            )
            raise HTTPException(status_code=503, detail="webhook_lifecycle_unavailable")
        if lifecycle_outcome == "mismatched":
            delivery_status = await _update_delivery_status(
                service,
                event.provider_id,
                event.delivery_id,
                status="rejected",
                processed_at=datetime.now(timezone.utc),
                error_code="pull_request_mismatched",
                expected_statuses=("processing",),
            )
        else:
            delivery_status = await _update_delivery_status(
                service,
                event.provider_id,
                event.delivery_id,
                status="processed",
                processed_at=datetime.now(timezone.utc),
                expected_statuses=("processing",),
            )
    elif result.is_duplicate and lifecycle_outcome is None:
        lifecycle_outcome = "duplicate"

    response: dict[str, Any] = {
        "delivery_id": event.delivery_id,
        "duplicate": duplicate,
        "status": delivery_status,
        "pull_request": {
            "provider_id": event.provider_id,
            "repository": event.repo,
            "number": event.number,
        },
    }
    if lifecycle_outcome is not None:
        response["lifecycle"] = lifecycle_outcome
    return response


def _webhook_metric_status(status: object, duplicate: object) -> str:
    if duplicate is True:
        return "duplicate"
    if isinstance(status, str) and status in {
        "received",
        "processed",
        "retryable",
        "reconcile_unavailable",
        "rejected",
    }:
        return status
    return "error"


async def _read_limited_body(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdigit():
            raise HTTPException(status_code=400, detail="invalid_content_length")
        normalized_length = declared_length.lstrip("0") or "0"
        if len(normalized_length) > len(str(MAX_WEBHOOK_BODY_BYTES)):
            raise HTTPException(status_code=413, detail="body_too_large")
        if int(normalized_length) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body_too_large")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_WEBHOOK_BODY_BYTES - len(body):
            raise HTTPException(status_code=413, detail="body_too_large")
        body.extend(chunk)
    return bytes(body)


async def _update_delivery_status(
    service: Any,
    provider_id: str,
    delivery_id: str,
    *,
    status: str,
    processed_at: datetime | None = None,
    error_code: str | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> str:
    repository = getattr(service, "delivery_repository", None)
    if not isinstance(repository, WebhookDeliveryRepository):
        return status
    delivery = await repository.update_webhook_delivery(
        provider_id,
        delivery_id,
        status=status,
        processed_at=processed_at,
        error_code=error_code,
        expected_statuses=expected_statuses,
    )
    if delivery is None:
        raise HTTPException(status_code=503, detail="webhook_delivery_update_unavailable")
    return delivery.status


def _error_status_code(
    error: WebhookProcessingError,
    provider_type: str,
    headers: Mapping[str, str],
) -> int:
    if error.code is WebhookErrorCode.MISSING_HEADER:
        signature_header = _SIGNATURE_HEADERS.get(provider_type.casefold())
        if signature_header is not None:
            signature = headers.get(signature_header)
            if signature is None or not signature.strip():
                return 401
        return 400
    return _ERROR_STATUS_CODES.get(error.code, 400)
