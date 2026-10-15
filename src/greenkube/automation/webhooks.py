"""Verify and persist bounded Git provider pull-request webhook deliveries.

The public contract accepts a trusted provider ID/type, the original HTTP
headers, and the exact raw request body. GitHub's ``X-Hub-Signature-256`` and
Gitea's ``X-Gitea-Signature`` authenticate the body with HMAC-SHA256; neither
signature contains a signed delivery timestamp. GitLab's ``X-Gitlab-Token``
authenticates only the shared token header and likewise supplies no signed
timestamp. Consequently this service does not reject deliveries based on a
timestamp in a payload or an unsigned header. Delivery-ID dedupe prevents
reprocessing retries that preserve their provider-assigned ID; GitHub and
Gitea HMACs do not cover event/delivery headers, so IDs are not signed replay
proofs.

Signature verification authenticates a provider, not a pull-request actor.
Payload ``sender``/``user`` fields are deliberately ignored; callers must
authorize repositories and users from trusted configuration, never from those
payload fields. GitLab's token header is not a body MAC, so deploy its webhook
endpoint over TLS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal, NoReturn, Protocol

from pydantic import ValidationError

from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import WebhookDelivery, WebhookDeliveryRepository

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")

PullRequestAction = Literal["opened", "synchronized", "closed", "reopened"]


class WebhookErrorCode(StrEnum):
    """Stable, secret-free error codes suitable for an HTTP/API boundary."""

    INVALID_BODY = "invalid_body"
    INVALID_HEADERS = "invalid_headers"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_PROVIDER = "invalid_provider"
    INVALID_PROVIDER_ID = "invalid_provider_id"
    INVALID_SIGNATURE = "invalid_signature"
    BODY_TOO_LARGE = "body_too_large"
    MISSING_HEADER = "missing_header"
    SECRET_UNAVAILABLE = "secret_unavailable"
    UNSUPPORTED_EVENT = "unsupported_event"


class WebhookProcessingError(ValueError):
    """A webhook rejection with a stable code and no echoed request secrets."""

    def __init__(self, code: WebhookErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class WebhookSecretResolver(Protocol):
    """Resolve the webhook secret for a trusted provider ID."""

    async def __call__(self, provider_id: str) -> str | None: ...


class WebhookProvider(StrEnum):
    GITHUB = "github"
    GITEA = "gitea"
    GITLAB = "gitlab"


@dataclass(frozen=True, slots=True)
class WebhookProcessingResult:
    """Persisted delivery plus whether this request created the dedupe record."""

    delivery: WebhookDelivery
    event: PullRequestEvent | None
    created: bool

    @property
    def should_process(self) -> bool:
        return self.delivery.status in {"received", "retryable", "reconcile_unavailable"}

    @property
    def is_duplicate(self) -> bool:
        return not self.created


class WebhookProcessingService:
    """Verify and normalize only supported GitHub, Gitea, and GitLab PR events.

    The service caps raw bodies at 1 MiB by default (a lower limit may be
    configured) and verifies signatures against the exact input bytes before
    JSON parsing. GitHub and Gitea use HMAC-SHA256 over the body; GitLab uses a
    constant-time comparison of ``X-Gitlab-Token`` with its configured secret.
    None of these provider-native checks includes a signed request timestamp,
    so freshness is not inferred from payload timestamps or unsigned headers.
    The provider delivery ID is persisted with the normalized event. A
    duplicate is eligible for reconciliation only while its stored status is
    non-terminal, allowing provider retries to recover transient failures.
    The size check runs before parsing; an HTTP caller should also bound its
    request-body read to avoid buffering an oversized request upstream.

    ``provider_id`` and ``provider_type`` must come from trusted configuration,
    not from the payload. This service does not authorize actors or repository
    access, and intentionally discards provider actor fields.
    """

    def __init__(
        self,
        *,
        secret_resolver: WebhookSecretResolver,
        delivery_repository: WebhookDeliveryRepository,
        max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    ) -> None:
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("max_body_bytes must be an integer")
        if not 1 <= max_body_bytes <= MAX_WEBHOOK_BODY_BYTES:
            raise ValueError(f"max_body_bytes must be between 1 and {MAX_WEBHOOK_BODY_BYTES}")
        self.secret_resolver = secret_resolver
        self.delivery_repository = delivery_repository
        self.max_body_bytes = max_body_bytes

    async def claim_for_processing(self, event: PullRequestEvent) -> bool:
        """Claim a verified persisted delivery for a bounded processing lease."""
        return await self.delivery_repository.claim_webhook_delivery(
            event.provider_id,
            event.delivery_id,
            stale_before=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

    async def process(
        self,
        *,
        provider_id: str,
        provider_type: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookProcessingResult:
        """Verify one raw webhook request and idempotently record its PR event.

        For GitHub, ``X-Hub-Signature-256`` must be ``sha256=<hex HMAC>`` and
        the dedupe ID is ``X-GitHub-Delivery``. For Gitea, the raw-hex
        ``X-Gitea-Signature`` HMAC is used with ``X-Gitea-Delivery``. GitLab
        compares ``X-Gitlab-Token`` in constant time and uses
        ``X-Gitlab-Event-UUID`` (not the webhook-configuration UUID) as its
        delivery ID. These schemes do not sign a delivery timestamp; retries
        are accepted regardless of event age and deduplicated by that ID.

        Raises :class:`WebhookProcessingError` for malformed, unauthenticated,
        oversized, or unsupported requests. Error messages contain only a
        stable code; secrets, signature values, and payload contents are never
        included.
        """

        if not isinstance(body, bytes):
            raise WebhookProcessingError(WebhookErrorCode.INVALID_BODY)
        if len(body) > self.max_body_bytes:
            raise WebhookProcessingError(WebhookErrorCode.BODY_TOO_LARGE)
        if not isinstance(headers, Mapping):
            raise WebhookProcessingError(WebhookErrorCode.INVALID_HEADERS)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise WebhookProcessingError(WebhookErrorCode.INVALID_PROVIDER_ID)

        try:
            provider = WebhookProvider(provider_type.casefold())
        except (AttributeError, ValueError):
            raise WebhookProcessingError(WebhookErrorCode.INVALID_PROVIDER) from None

        trusted_provider_id = provider_id.strip()
        try:
            secret = await self.secret_resolver(trusted_provider_id)
        except Exception:
            raise WebhookProcessingError(WebhookErrorCode.SECRET_UNAVAILABLE) from None
        if not isinstance(secret, str) or not secret:
            raise WebhookProcessingError(WebhookErrorCode.SECRET_UNAVAILABLE)

        _verify_signature(provider, headers, body, secret)
        event_type = _required_header(headers, _event_header(provider))
        delivery_id = _required_header(headers, _delivery_header(provider))

        payload = _parse_json(body)
        event = _normalize_event(provider, trusted_provider_id, delivery_id, event_type, payload)
        delivery, created = await self.delivery_repository.record_webhook_delivery(
            event,
            event_type="pull_request",
        )
        return WebhookProcessingResult(delivery=delivery, event=delivery.event, created=created)


def _event_header(provider: WebhookProvider) -> str:
    if provider is WebhookProvider.GITHUB:
        return "X-GitHub-Event"
    if provider is WebhookProvider.GITEA:
        return "X-Gitea-Event"
    return "X-Gitlab-Event"


def _delivery_header(provider: WebhookProvider) -> str:
    if provider is WebhookProvider.GITHUB:
        return "X-GitHub-Delivery"
    if provider is WebhookProvider.GITEA:
        return "X-Gitea-Delivery"
    return "X-Gitlab-Event-UUID"


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = _header(headers, name)
    if value is None or not value:
        raise WebhookProcessingError(WebhookErrorCode.MISSING_HEADER)
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    matching: list[str] = []
    normalized_name = name.casefold()
    for header_name, value in headers.items():
        if not isinstance(header_name, str) or not isinstance(value, str):
            raise WebhookProcessingError(WebhookErrorCode.INVALID_HEADERS)
        if header_name.casefold() == normalized_name:
            matching.append(value.strip())
    if len(matching) > 1:
        raise WebhookProcessingError(WebhookErrorCode.INVALID_HEADERS)
    return matching[0] if matching else None


def _verify_signature(
    provider: WebhookProvider,
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
) -> None:
    if provider is WebhookProvider.GITHUB:
        signature = _required_header(headers, "X-Hub-Signature-256")
        algorithm, separator, supplied_digest = signature.partition("=")
        if algorithm != "sha256" or not separator or _HEX_SHA256.fullmatch(supplied_digest) is None:
            raise WebhookProcessingError(WebhookErrorCode.INVALID_SIGNATURE)
        supplied = bytes.fromhex(supplied_digest)
        expected = hmac.digest(secret.encode("utf-8"), body, hashlib.sha256)
    elif provider is WebhookProvider.GITEA:
        signature = _required_header(headers, "X-Gitea-Signature")
        if _HEX_SHA256.fullmatch(signature) is None:
            raise WebhookProcessingError(WebhookErrorCode.INVALID_SIGNATURE)
        supplied = bytes.fromhex(signature)
        expected = hmac.digest(secret.encode("utf-8"), body, hashlib.sha256)
    else:
        token = _required_header(headers, "X-Gitlab-Token")
        supplied = token.encode("utf-8")
        expected = secret.encode("utf-8")

    if not hmac.compare_digest(expected, supplied):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_SIGNATURE)


def _parse_json(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_BODY) from None
    if not isinstance(parsed, dict):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_BODY)
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    del value
    raise ValueError


def _normalize_event(
    provider: WebhookProvider,
    provider_id: str,
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> PullRequestEvent:
    if provider is WebhookProvider.GITHUB:
        if event_type.casefold() != "pull_request":
            raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)
        return _normalize_github_or_gitea(provider_id, delivery_id, payload)
    if provider is WebhookProvider.GITEA:
        if event_type.casefold() != "pull_request":
            raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)
        return _normalize_github_or_gitea(provider_id, delivery_id, payload)
    if event_type.casefold() != "merge request hook":
        raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)
    return _normalize_gitlab(provider_id, delivery_id, payload)


def _normalize_github_or_gitea(
    provider_id: str,
    delivery_id: str,
    payload: dict[str, Any],
) -> PullRequestEvent:
    action = _pull_request_action(
        payload.get("action"),
        provider_action_map={
            "opened": "opened",
            "synchronize": "synchronized",
            "synchronized": "synchronized",
            "closed": "closed",
            "reopened": "reopened",
        },
    )
    pull_request = _required_mapping(payload, "pull_request")
    repository = _required_mapping(payload, "repository")
    head = _optional_mapping(pull_request, "head")
    base = _optional_mapping(pull_request, "base")
    return _make_event(
        provider_id=provider_id,
        delivery_id=delivery_id,
        repo=_required_string(repository, "full_name"),
        number=_required_positive_integer(pull_request, "number"),
        action=action,
        merged=_optional_boolean(pull_request, "merged", default=False),
        merge_commit_sha=_optional_string(pull_request, "merge_commit_sha"),
        head_sha=_optional_string(head, "sha") if head is not None else None,
        base_sha=_optional_string(base, "sha") if base is not None else None,
        occurred_at=_first_present(
            pull_request.get("updated_at"),
            pull_request.get("closed_at"),
            pull_request.get("created_at"),
        ),
    )


def _normalize_gitlab(
    provider_id: str,
    delivery_id: str,
    payload: dict[str, Any],
) -> PullRequestEvent:
    if payload.get("object_kind") != "merge_request":
        raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)

    attributes = _required_mapping(payload, "object_attributes")
    project = _required_mapping(payload, "project")
    raw_action = attributes.get("action")
    action = _pull_request_action(
        raw_action,
        provider_action_map={
            "open": "opened",
            "opened": "opened",
            "update": "synchronized",
            "synchronize": "synchronized",
            "synchronized": "synchronized",
            "close": "closed",
            "closed": "closed",
            "reopen": "reopened",
            "reopened": "reopened",
            "merge": "closed",
        },
    )
    state = _optional_string(attributes, "state")
    merged_at = _optional_string(attributes, "merged_at")
    last_commit = _optional_mapping(attributes, "last_commit")
    diff_refs = _optional_mapping(attributes, "diff_refs")
    return _make_event(
        provider_id=provider_id,
        delivery_id=delivery_id,
        repo=_required_string(project, "path_with_namespace"),
        number=_required_positive_integer(attributes, "iid"),
        action=action,
        merged=(isinstance(raw_action, str) and raw_action.casefold() == "merge")
        or state == "merged"
        or merged_at is not None,
        merge_commit_sha=_optional_string(attributes, "merge_commit_sha"),
        head_sha=(
            _optional_string(last_commit, "id")
            if last_commit is not None
            else _optional_string(attributes, "last_commit_id")
        ),
        base_sha=_optional_string(diff_refs, "base_sha") if diff_refs is not None else None,
        occurred_at=_first_present(
            attributes.get("updated_at"),
            attributes.get("merged_at"),
            attributes.get("created_at"),
        ),
    )


def _make_event(
    *,
    provider_id: str,
    delivery_id: str,
    repo: str,
    number: int,
    action: PullRequestAction,
    merged: bool,
    merge_commit_sha: str | None,
    head_sha: str | None,
    base_sha: str | None,
    occurred_at: Any,
) -> PullRequestEvent:
    values: dict[str, Any] = {
        "provider_id": provider_id,
        "delivery_id": delivery_id,
        "repo": repo,
        "number": number,
        "action": action,
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "head_sha": head_sha,
        "base_sha": base_sha,
    }
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    try:
        return PullRequestEvent.model_validate(values)
    except (ValidationError, TypeError, ValueError):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD) from None


def _pull_request_action(
    value: Any,
    *,
    provider_action_map: Mapping[str, PullRequestAction],
) -> PullRequestAction:
    if not isinstance(value, str):
        raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)
    normalized = provider_action_map.get(value.casefold())
    if normalized is None:
        raise WebhookProcessingError(WebhookErrorCode.UNSUPPORTED_EVENT)
    return normalized


def _required_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value


def _optional_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value or None


def _required_positive_integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value


def _optional_boolean(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise WebhookProcessingError(WebhookErrorCode.INVALID_PAYLOAD)
    return value


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)
