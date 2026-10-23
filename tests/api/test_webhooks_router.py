"""HTTP boundary tests for provider pull-request webhooks."""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import _is_webhook_ingress_path, get_gitops_binding_repository
from greenkube.automation.webhooks import (
    MAX_WEBHOOK_BODY_BYTES,
    WebhookErrorCode,
    WebhookProcessingError,
    WebhookProcessingResult,
    WebhookProcessingService,
)
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    WebhookDelivery,
    WebhookDeliveryRepository,
)

_WEBHOOK_SECRET = "provider-native-webhook-secret"


@dataclass(frozen=True)
class _ProcessingCall:
    provider_id: str
    provider_type: str
    headers: Mapping[str, str]
    body: bytes


class _ProviderRepository:
    def __init__(
        self,
        provider: GitOpsProviderConfig | None = GitOpsProviderConfig(
            id="provider-main",
            provider_type="gitea",
        ),
        error: Exception | None = None,
    ) -> None:
        self.provider = provider
        self.error = error
        self.lookups: list[str] = []

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        self.lookups.append(provider_id)
        if self.error is not None:
            raise self.error
        if self.provider is None or self.provider.id != provider_id:
            return None
        return self.provider


class _MemoryWebhookDeliveryRepository(WebhookDeliveryRepository):
    def __init__(self) -> None:
        self.deliveries: dict[tuple[str, str], WebhookDelivery] = {}
        self.record_calls = 0

    async def record_webhook_delivery(
        self,
        event: PullRequestEvent,
        *,
        event_type: str = "pull_request",
    ) -> tuple[WebhookDelivery, bool]:
        self.record_calls += 1
        key = (event.provider_id, event.delivery_id)
        existing = self.deliveries.get(key)
        if existing is not None:
            return existing, False
        delivery = WebhookDelivery(
            id=len(self.deliveries) + 1,
            event_type=event_type,
            event=event,
            status="received",
            received_at=datetime.now(timezone.utc),
        )
        self.deliveries[key] = delivery
        return delivery, True

    async def get_webhook_delivery(self, provider_id: str, delivery_id: str) -> WebhookDelivery | None:
        return self.deliveries.get((provider_id, delivery_id))

    async def update_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        status: str,
        processed_at: datetime | None = None,
        error_code: str | None = None,
        expected_statuses: Sequence[str] | None = None,
    ) -> WebhookDelivery | None:
        delivery = await self.get_webhook_delivery(provider_id, delivery_id)
        if delivery is None or (expected_statuses is not None and delivery.status not in expected_statuses):
            return None
        updated = WebhookDelivery(
            id=delivery.id,
            event_type=delivery.event_type,
            event=delivery.event,
            status=status,
            received_at=delivery.received_at,
            processed_at=processed_at,
            error_code=error_code,
        )
        self.deliveries[(provider_id, delivery_id)] = updated
        return updated

    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool:
        delivery = await self.get_webhook_delivery(provider_id, delivery_id)
        if delivery is None or delivery.status not in {"received", "retryable", "reconcile_unavailable"}:
            return False
        self.deliveries[(provider_id, delivery_id)] = WebhookDelivery(
            id=delivery.id,
            event_type=delivery.event_type,
            event=delivery.event,
            status="processing",
            received_at=delivery.received_at,
        )
        return True

    async def list_pending_webhook_deliveries(
        self,
        *,
        limit: int,
        stale_before: datetime,
    ) -> list[WebhookDelivery]:
        return [
            delivery
            for delivery in list(self.deliveries.values())[:limit]
            if delivery.status in {"received", "retryable", "reconcile_unavailable"}
        ]


class _RecordingWebhookService:
    def __init__(
        self,
        result: WebhookProcessingResult | None = None,
        error: WebhookProcessingError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[_ProcessingCall] = []

    async def process(
        self,
        *,
        provider_id: str,
        provider_type: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookProcessingResult:
        self.calls.append(
            _ProcessingCall(
                provider_id=provider_id,
                provider_type=provider_type,
                headers=headers,
                body=body,
            )
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def claim_for_processing(self, event: PullRequestEvent) -> bool:
        return True


class _RecordingLifecycleReconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[PullRequestEvent, bool]] = []

    async def reconcile(self, event: PullRequestEvent, *, newly_deduplicated: bool):
        self.calls.append((event, newly_deduplicated))
        return SimpleNamespace(outcome=SimpleNamespace(value="merged"))


def _processing_result(
    *,
    created: bool = True,
    status: str = "received",
    action: Literal["opened", "synchronized", "closed", "reopened"] = "opened",
) -> WebhookProcessingResult:
    event = PullRequestEvent(
        provider_id="provider-main",
        delivery_id="delivery-42",
        repo="acme/platform",
        number=42,
        action=action,
        merged=False,
        head_sha="head-sha-42",
        base_sha="base-sha-42",
    )
    delivery = WebhookDelivery(
        id=42,
        event_type="pull_request",
        event=event,
        status=status,
        received_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    return WebhookProcessingResult(delivery=delivery, event=event, created=created)


def _configure_app(monkeypatch: pytest.MonkeyPatch, *, api_key: str = ""):
    from greenkube.core.config import Config

    config = Config(ENVIRONMENT="test", GREENKUBE_API_KEY=api_key)
    monkeypatch.setattr("greenkube.core.config.config", config)
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: config)
    monkeypatch.setattr("greenkube.api.app.get_config", lambda: config)
    app = create_app()
    provider_repository = _ProviderRepository()
    app.dependency_overrides[get_gitops_binding_repository] = lambda: provider_repository
    return app, provider_repository


def _native_webhook_payload(
    *,
    action: str = "opened",
    merged: bool = False,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "action": action,
            "number": 42,
            "repository": {"full_name": "acme/platform"},
            "pull_request": {
                "number": 42,
                "merged": merged,
                "merge_commit_sha": "merge-sha-42" if merged else None,
                "head": {"sha": "head-sha-42"},
                "base": {"sha": "base-sha-42"},
            },
            "sender": {"login": "untrusted-payload-actor"},
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-Gitea-Event": "pull_request",
        "X-Gitea-Delivery": "native-delivery-42",
        "X-Gitea-Signature": signature,
    }


def _configure_native_webhook_app(monkeypatch: pytest.MonkeyPatch):
    app, provider_repository = _configure_app(monkeypatch, api_key="test-webhook-api-key")
    delivery_repository = _MemoryWebhookDeliveryRepository()

    async def resolve_secret(provider_id: str) -> str | None:
        return _WEBHOOK_SECRET if provider_id == "provider-main" else None

    app.state.webhook_processing_service = WebhookProcessingService(
        secret_resolver=resolve_secret,
        delivery_repository=delivery_repository,
    )
    return app, provider_repository, delivery_repository


@pytest.fixture
def webhook_client(monkeypatch: pytest.MonkeyPatch):
    app, provider_repository = _configure_app(monkeypatch)
    with TestClient(app) as client:
        yield client, app, provider_repository
    app.dependency_overrides.clear()


def test_route_uses_persisted_provider_type_and_returns_only_safe_correlation(webhook_client):
    client, app, provider_repository = webhook_client
    service = _RecordingWebhookService(_processing_result())
    app.state.webhook_processing_service = service
    body = b'{"provider_type":"github","sender":{"login":"payload-actor"},"token":"payload-secret"}'
    headers = {
        "X-Gitea-Event": "pull_request",
        "X-Gitea-Delivery": "delivery-42",
        "X-Gitea-Signature": "provider-signature",
    }

    response = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)

    assert response.status_code == 200
    assert provider_repository.lookups == ["provider-main"]
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call.provider_id == "provider-main"
    assert call.provider_type == "gitea"
    assert call.body == body
    assert call.headers["x-gitea-event"] == "pull_request"
    assert call.headers["x-gitea-delivery"] == "delivery-42"
    assert call.headers["x-gitea-signature"] == "provider-signature"
    response_body = response.json()
    assert response_body == {
        "delivery_id": "delivery-42",
        "duplicate": False,
        "status": "processed",
        "pull_request": {
            "provider_id": "provider-main",
            "repository": "acme/platform",
            "number": 42,
        },
        "lifecycle": "ignored",
    }
    assert "payload-actor" not in response.text
    assert "payload-secret" not in response.text
    assert body.decode() not in response.text


def test_duplicate_delivery_uses_persisted_status_and_duplicate_flag(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(
        _processing_result(created=False, status="processed")
    )

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["status"] == "processed"


def test_missing_processor_or_provider_configuration_returns_503(webhook_client):
    client, app, provider_repository = webhook_client

    missing_processor = client.post("/api/v1/webhooks/provider-main", content=b"{}")
    assert missing_processor.status_code == 503

    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result())
    provider_repository.provider = None
    missing_configuration = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert missing_configuration.status_code == 503
    assert app.state.webhook_processing_service.calls == []


def test_new_closed_webhook_is_reconciled_and_delivery_marked_processed(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result(action="closed"))
    reconciler = _RecordingLifecycleReconciler()
    app.state.pull_request_lifecycle_reconciler = reconciler

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 200
    assert len(reconciler.calls) == 1
    assert reconciler.calls[0][1] is True
    assert reconciler.calls[0][0].action == "closed"
    assert response.json()["status"] == "processed"
    assert response.json()["lifecycle"] == "merged"
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_webhook_deliveries_total{status="processed"}' in output
    assert "greenkube_webhook_delivery_lag_seconds_count" in output
    assert 'greenkube_pull_requests_total{provider="gitea",state="merged"}' in output


def test_duplicate_closed_webhook_is_not_reconciled_again(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(
        _processing_result(created=False, status="processed", action="closed")
    )
    reconciler = _RecordingLifecycleReconciler()
    app.state.pull_request_lifecycle_reconciler = reconciler

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["lifecycle"] == "duplicate"
    assert reconciler.calls == []


def test_duplicate_nonterminal_webhook_retries_lifecycle_reconciliation(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(
        _processing_result(created=False, status="retryable", action="closed")
    )
    reconciler = _RecordingLifecycleReconciler()
    app.state.pull_request_lifecycle_reconciler = reconciler

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["status"] == "processed"
    assert len(reconciler.calls) == 1
    assert reconciler.calls[0][1] is True


def test_closed_webhook_is_not_acked_without_lifecycle_runtime(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result(action="closed"))

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 503
    assert response.json()["detail"] == "webhook_lifecycle_unavailable"


def test_open_webhook_is_marked_terminal_without_lifecycle_runtime(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result(action="opened"))

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert response.json()["lifecycle"] == "ignored"


def test_unlinked_webhook_remains_retryable(webhook_client):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result(action="closed"))

    async def reconcile(event, *, newly_deduplicated):
        return SimpleNamespace(outcome=SimpleNamespace(value="unlinked"))

    app.state.pull_request_lifecycle_reconciler = SimpleNamespace(reconcile=reconcile)

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 503
    assert response.json()["detail"] == "webhook_lifecycle_unavailable"


@pytest.mark.parametrize(
    ("code", "headers", "expected_status"),
    [
        (WebhookErrorCode.INVALID_SIGNATURE, {}, 401),
        (WebhookErrorCode.MISSING_HEADER, {}, 401),
        (WebhookErrorCode.MISSING_HEADER, {"X-Gitea-Signature": "signature"}, 400),
        (WebhookErrorCode.INVALID_BODY, {}, 400),
        (WebhookErrorCode.INVALID_HEADERS, {}, 400),
        (WebhookErrorCode.INVALID_PAYLOAD, {}, 400),
        (WebhookErrorCode.INVALID_PROVIDER, {}, 400),
        (WebhookErrorCode.UNSUPPORTED_EVENT, {}, 400),
        (WebhookErrorCode.BODY_TOO_LARGE, {}, 413),
        (WebhookErrorCode.SECRET_UNAVAILABLE, {}, 503),
    ],
)
def test_processing_errors_map_to_safe_http_statuses(webhook_client, code, headers, expected_status):
    client, app, _ = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(error=WebhookProcessingError(code))

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}", headers=headers)

    assert response.status_code == expected_status
    assert "provider-signature" not in response.text
    assert "payload-secret" not in response.text


def test_oversized_stream_is_rejected_without_calling_processor(webhook_client):
    client, app, _ = webhook_client
    service = _RecordingWebhookService(_processing_result())
    app.state.webhook_processing_service = service

    response = client.post(
        "/api/v1/webhooks/provider-main",
        content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
    )

    assert response.status_code == 413
    assert service.calls == []


def test_body_at_limit_is_forwarded_exactly(webhook_client):
    client, app, _ = webhook_client
    service = _RecordingWebhookService(_processing_result())
    app.state.webhook_processing_service = service
    body = b"x" * MAX_WEBHOOK_BODY_BYTES

    response = client.post("/api/v1/webhooks/provider-main", content=body)

    assert response.status_code == 200
    assert service.calls[0].body == body


def test_chunked_oversized_stream_is_rejected_without_calling_processor(webhook_client):
    client, app, _ = webhook_client
    service = _RecordingWebhookService(_processing_result())
    app.state.webhook_processing_service = service

    def chunks():
        yield b"x" * MAX_WEBHOOK_BODY_BYTES
        yield b"x"

    response = client.post("/api/v1/webhooks/provider-main", content=chunks())

    assert response.status_code == 413
    assert service.calls == []


def test_provider_lookup_failure_is_redacted_and_returns_503(webhook_client):
    client, app, provider_repository = webhook_client
    app.state.webhook_processing_service = _RecordingWebhookService(_processing_result())
    provider_repository.error = RuntimeError("private-provider-configuration")

    response = client.post("/api/v1/webhooks/provider-main", content=b"{}")

    assert response.status_code == 503
    assert "private-provider-configuration" not in response.text
    assert app.state.webhook_processing_service.calls == []


def test_native_signature_authenticates_webhook_without_application_api_key(monkeypatch: pytest.MonkeyPatch):
    app, provider_repository, delivery_repository = _configure_native_webhook_app(monkeypatch)
    body, headers = _native_webhook_payload()

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)

    assert response.status_code == 200
    assert provider_repository.lookups == ["provider-main"]
    assert delivery_repository.record_calls == 1
    assert response.json()["delivery_id"] == "native-delivery-42"


def test_valid_native_webhook_dispatches_reconciler_only_after_persistence(monkeypatch: pytest.MonkeyPatch):
    app, _, delivery_repository = _configure_native_webhook_app(monkeypatch)
    reconciler = _RecordingLifecycleReconciler()
    app.state.pull_request_lifecycle_reconciler = reconciler
    body, headers = _native_webhook_payload()

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)
        replay = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)

    assert response.status_code == 200
    assert replay.status_code == 200
    assert delivery_repository.record_calls == 2
    assert len(reconciler.calls) == 1
    assert reconciler.calls[0][0].delivery_id == "native-delivery-42"
    assert reconciler.calls[0][1] is True
    assert response.json()["status"] == "processed"
    assert replay.json()["duplicate"] is True


def test_failed_webhook_reconciliation_retries_same_delivery_to_terminal_state(monkeypatch: pytest.MonkeyPatch):
    app, _, delivery_repository = _configure_native_webhook_app(monkeypatch)

    class FailsOnceReconciler:
        calls = 0

        async def reconcile(self, event: PullRequestEvent, *, newly_deduplicated: bool):
            assert newly_deduplicated is True
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient provider timeout")
            return SimpleNamespace(outcome=SimpleNamespace(value="merged"))

    reconciler = FailsOnceReconciler()
    app.state.pull_request_lifecycle_reconciler = reconciler
    body, headers = _native_webhook_payload(action="closed", merged=True)

    with TestClient(app) as client:
        failed = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)
        retried = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)

    delivery = delivery_repository.deliveries[("provider-main", "native-delivery-42")]
    assert failed.status_code == 503
    assert retried.status_code == 200
    assert retried.json()["duplicate"] is True
    assert retried.json()["status"] == "processed"
    assert reconciler.calls == 2
    assert delivery.status == "processed"


def test_invalid_native_signature_fails_without_application_api_key(monkeypatch: pytest.MonkeyPatch):
    app, _, delivery_repository = _configure_native_webhook_app(monkeypatch)
    body, headers = _native_webhook_payload()
    headers["X-Gitea-Signature"] = "0" * 64

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/provider-main", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == WebhookErrorCode.INVALID_SIGNATURE.value
    assert delivery_repository.record_calls == 0


def test_only_provider_webhook_route_is_exempt_from_application_api_key(monkeypatch: pytest.MonkeyPatch):
    app, _ = _configure_app(monkeypatch, api_key="test-webhook-api-key")

    with TestClient(app) as client:
        protected_api = client.get("/api/v1/health/services")

    assert protected_api.status_code == 401
    assert _is_webhook_ingress_path("/api/v1/webhooks/provider-main")
    assert not _is_webhook_ingress_path("/api/v1/webhooks/")
    assert not _is_webhook_ingress_path("/api/v1/webhooks/provider-main/extra")
    assert not _is_webhook_ingress_path("/api/v1/webhooks2/provider-main")
