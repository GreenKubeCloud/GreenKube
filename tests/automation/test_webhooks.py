from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from greenkube.automation import webhooks
from greenkube.automation.webhooks import (
    WebhookErrorCode,
    WebhookProcessingError,
    WebhookProcessingService,
)
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import WebhookDelivery, WebhookDeliveryRepository


class MemoryWebhookDeliveryRepository(WebhookDeliveryRepository):
    def __init__(self) -> None:
        self.deliveries: dict[tuple[str, str], WebhookDelivery] = {}
        self.record_calls = 0

    async def record_webhook_delivery(
        self, event: PullRequestEvent, *, event_type: str = "pull_request"
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
        del processed_at, error_code
        key = (provider_id, delivery_id)
        delivery = self.deliveries.get(key)
        if delivery is None or (expected_statuses is not None and delivery.status not in expected_statuses):
            return None
        updated = WebhookDelivery(
            id=delivery.id,
            event_type=delivery.event_type,
            event=delivery.event,
            status=status,
            received_at=delivery.received_at,
        )
        self.deliveries[key] = updated
        return updated

    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool:
        key = (provider_id, delivery_id)
        delivery = self.deliveries.get(key)
        if delivery is None:
            return False
        if delivery.status not in {"received", "retryable", "reconcile_unavailable"} and not (
            delivery.status == "processing" and delivery.updated_at is not None and delivery.updated_at < stale_before
        ):
            return False
        self.deliveries[key] = WebhookDelivery(
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
            or (
                delivery.status == "processing"
                and delivery.updated_at is not None
                and delivery.updated_at < stale_before
            )
        ]


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _github_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _gitea_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _github_payload(*, action: str = "opened") -> dict[str, object]:
    return {
        "action": action,
        "number": 42,
        "repository": {"full_name": "acme/platform"},
        "pull_request": {
            "number": 42,
            "merged": False,
            "merge_commit_sha": None,
            "head": {"sha": "head-sha"},
            "base": {"sha": "base-sha"},
            "updated_at": "2000-01-01T00:00:00Z",
        },
        "sender": {"login": "untrusted-payload-actor"},
    }


def _service(
    *,
    secrets: dict[str, str] | None = None,
    repository: MemoryWebhookDeliveryRepository | None = None,
    max_body_bytes: int = 1024 * 1024,
) -> tuple[WebhookProcessingService, MemoryWebhookDeliveryRepository, list[str]]:
    configured_secrets = secrets if secrets is not None else {"provider-github": "github-secret"}
    resolved_for: list[str] = []

    async def resolve_secret(provider_id: str) -> str | None:
        resolved_for.append(provider_id)
        return configured_secrets.get(provider_id)

    delivery_repository = repository or MemoryWebhookDeliveryRepository()
    service = WebhookProcessingService(
        secret_resolver=resolve_secret,
        delivery_repository=delivery_repository,
        max_body_bytes=max_body_bytes,
    )
    return service, delivery_repository, resolved_for


@pytest.mark.asyncio
async def test_github_validates_raw_body_and_deduplicates_delivery_ids():
    secret = "github-secret"
    body = _body(_github_payload())
    service, repository, resolved_for = _service()
    headers = {
        "x-github-event": "pull_request",
        "X-GitHub-Delivery": "delivery-github-1",
        "X-Hub-Signature-256": _github_signature(body, secret),
    }

    first = await service.process(
        provider_id="provider-github",
        provider_type="github",
        headers=headers,
        body=body,
    )
    await repository.update_webhook_delivery(
        "provider-github",
        "delivery-github-1",
        status="processed",
        processed_at=datetime.now(timezone.utc),
    )
    replay_body = _body(_github_payload(action="closed"))
    replay_headers = {
        **headers,
        "X-Hub-Signature-256": _github_signature(replay_body, secret),
    }
    duplicate = await service.process(
        provider_id="provider-github",
        provider_type="github",
        headers=replay_headers,
        body=replay_body,
    )

    assert first.created is True
    assert first.should_process is True
    assert first.event is not None
    assert first.event.provider_id == "provider-github"
    assert first.event.delivery_id == "delivery-github-1"
    assert first.event.repo == "acme/platform"
    assert first.event.action == "opened"
    assert first.event.head_sha == "head-sha"
    assert first.event.base_sha == "base-sha"
    assert first.event.occurred_at == datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert "sender" not in first.event.model_dump()

    assert duplicate.created is False
    assert duplicate.should_process is False
    assert duplicate.is_duplicate is True
    assert duplicate.event == first.event
    assert len(repository.deliveries) == 1
    assert repository.record_calls == 2
    assert resolved_for == ["provider-github", "provider-github"]


@pytest.mark.asyncio
async def test_duplicate_nonterminal_delivery_is_eligible_for_retry():
    secret = "github-secret"
    body = _body(_github_payload(action="closed"))
    service, repository, _ = _service()
    headers = {
        "x-github-event": "pull_request",
        "X-GitHub-Delivery": "delivery-github-retry",
        "X-Hub-Signature-256": _github_signature(body, secret),
    }

    first = await service.process(
        provider_id="provider-github",
        provider_type="github",
        headers=headers,
        body=body,
    )
    await repository.update_webhook_delivery(
        "provider-github",
        "delivery-github-retry",
        status="retryable",
        error_code="lifecycle_reconcile_failed",
    )

    retry = await service.process(
        provider_id="provider-github",
        provider_type="github",
        headers=headers,
        body=body,
    )

    assert first.created is True
    assert retry.created is False
    assert retry.should_process is True
    assert retry.event == first.event


@pytest.mark.asyncio
async def test_github_rejects_tampered_body_without_exposing_secret():
    secret = "do-not-leak-this-secret"
    body = _body(_github_payload())
    service, repository, _ = _service(secrets={"provider-github": secret})
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-github-2",
        "X-Hub-Signature-256": _github_signature(body, secret),
    }

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-github",
            provider_type="github",
            headers=headers,
            body=body + b" ",
        )

    assert exc_info.value.code is WebhookErrorCode.INVALID_SIGNATURE
    assert secret not in str(exc_info.value)
    assert not repository.deliveries


@pytest.mark.asyncio
async def test_gitea_verifies_native_hex_hmac_and_normalizes_synchronize_action():
    secret = "gitea-secret"
    body = _body(
        {
            "action": "synchronize",
            "number": 7,
            "repository": {"full_name": "team/api"},
            "pull_request": {
                "number": 7,
                "merged": False,
                "head": {"sha": "head-7"},
                "base": {"sha": "base-7"},
            },
        }
    )
    service, repository, _ = _service(secrets={"provider-gitea": secret})

    result = await service.process(
        provider_id="provider-gitea",
        provider_type="gitea",
        headers={
            "X-Gitea-Event": "pull_request",
            "X-Gitea-Delivery": "delivery-gitea-1",
            "X-Gitea-Signature": _gitea_signature(body, secret),
        },
        body=body,
    )

    assert result.created is True
    assert result.event is not None
    assert result.event.action == "synchronized"
    assert result.event.repo == "team/api"
    assert result.event.number == 7
    assert len(repository.deliveries) == 1


@pytest.mark.asyncio
async def test_gitlab_compares_token_in_constant_time_and_normalizes_merge_request(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "gitlab-webhook-token"
    body = _body(
        {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "team/api"},
            "object_attributes": {
                "iid": 12,
                "action": "merge",
                "state": "merged",
                "merge_commit_sha": "merge-12",
                "last_commit": {"id": "head-12"},
                "updated_at": "2026-10-20T10:00:00Z",
            },
            "user": {"username": "untrusted-payload-actor"},
        }
    )
    service, repository, _ = _service(secrets={"provider-gitlab": secret})
    original_compare_digest = webhooks.hmac.compare_digest
    comparisons: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return original_compare_digest(left, right)

    monkeypatch.setattr(webhooks.hmac, "compare_digest", compare_digest)
    result = await service.process(
        provider_id="provider-gitlab",
        provider_type="gitlab",
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Event-UUID": "delivery-gitlab-1",
            "X-Gitlab-Token": secret,
        },
        body=body,
    )

    assert (secret.encode(), secret.encode()) in comparisons
    assert result.event is not None
    assert result.event.action == "closed"
    assert result.event.merged is True
    assert result.event.merge_commit_sha == "merge-12"
    assert result.event.repo == "team/api"
    assert len(repository.deliveries) == 1


@pytest.mark.asyncio
async def test_gitlab_rejects_a_wrong_token_without_echoing_either_token():
    configured_secret = "configured-secret"
    supplied_token = "wrong-token"
    body = _body({"object_kind": "merge_request"})
    service, repository, _ = _service(secrets={"provider-gitlab": configured_secret})

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-gitlab",
            provider_type="gitlab",
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "delivery-gitlab-invalid",
                "X-Gitlab-Token": supplied_token,
            },
            body=body,
        )

    assert exc_info.value.code is WebhookErrorCode.INVALID_SIGNATURE
    assert configured_secret not in str(exc_info.value)
    assert supplied_token not in str(exc_info.value)
    assert not repository.deliveries


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_before_secret_resolution():
    service, repository, resolved_for = _service(max_body_bytes=32)

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-github",
            provider_type="github",
            headers={},
            body=b"x" * 33,
        )

    assert exc_info.value.code is WebhookErrorCode.BODY_TOO_LARGE
    assert resolved_for == []
    assert not repository.deliveries


@pytest.mark.asyncio
async def test_missing_delivery_id_is_rejected_instead_of_storing_undedupeable_event():
    secret = "github-secret"
    body = _body(_github_payload())
    service, repository, _ = _service()

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-github",
            provider_type="github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": _github_signature(body, secret),
            },
            body=body,
        )

    assert exc_info.value.code is WebhookErrorCode.MISSING_HEADER
    assert not repository.deliveries


@pytest.mark.asyncio
async def test_secret_resolution_errors_are_redacted():
    _, repository, _ = _service()

    async def broken_secret_resolver(provider_id: str) -> str | None:
        del provider_id
        raise RuntimeError("could not load private-secret-material")

    service = WebhookProcessingService(
        secret_resolver=broken_secret_resolver,
        delivery_repository=repository,
    )
    body = _body(_github_payload())

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-github",
            provider_type="github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-github-3",
                "X-Hub-Signature-256": _github_signature(body, "github-secret"),
            },
            body=body,
        )

    assert exc_info.value.code is WebhookErrorCode.SECRET_UNAVAILABLE
    assert "private-secret-material" not in str(exc_info.value)
    assert not repository.deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "action"),
    [("issues", "opened"), ("pull_request", "edited")],
)
async def test_only_supported_pull_request_event_types_and_actions_are_recorded(event_type: str, action: str):
    body = _body(_github_payload(action=action))
    secret = "github-secret"
    service, repository, _ = _service()

    with pytest.raises(WebhookProcessingError) as exc_info:
        await service.process(
            provider_id="provider-github",
            provider_type="github",
            headers={
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": "delivery-github-unsupported",
                "X-Hub-Signature-256": _github_signature(body, secret),
            },
            body=body,
        )

    assert exc_info.value.code is WebhookErrorCode.UNSUPPORTED_EVENT
    assert not repository.deliveries


@pytest.mark.asyncio
async def test_provider_id_and_delivery_id_are_not_taken_from_payload():
    secret = "github-secret"
    body = _body(
        {
            **_github_payload(),
            "provider_id": "attacker-provider",
            "delivery_id": "attacker-delivery",
        }
    )
    service, _, _ = _service()

    result = await service.process(
        provider_id="provider-github",
        provider_type="github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "trusted-header-delivery",
            "X-Hub-Signature-256": _github_signature(body, secret),
        },
        body=body,
    )

    assert result.event is not None
    assert result.event.provider_id == "provider-github"
    assert result.event.delivery_id == "trusted-header-delivery"
