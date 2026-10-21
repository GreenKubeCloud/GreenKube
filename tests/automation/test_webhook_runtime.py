"""Runtime wiring for mounted provider webhook secrets."""

import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from greenkube.automation.webhook_runtime import (
    MountedWebhookSecretResolver,
    WebhookSecretResolutionError,
    create_webhook_processing_service,
)
from greenkube.automation.webhooks import WebhookProcessingError
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import WebhookDelivery, WebhookDeliveryRepository


class _DeliveryRepository(WebhookDeliveryRepository):
    def __init__(self) -> None:
        self.deliveries: dict[tuple[str, str], WebhookDelivery] = {}

    async def record_webhook_delivery(
        self,
        event: PullRequestEvent,
        *,
        event_type: str = "pull_request",
    ) -> tuple[WebhookDelivery, bool]:
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
            processed_at=processed_at,
            error_code=error_code,
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
        delivery = self.deliveries.get((provider_id, delivery_id))
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
            for delivery in self.deliveries.values()
            if delivery.status in {"received", "retryable", "reconcile_unavailable"}
        ][:limit]


@pytest.mark.asyncio
async def test_mounted_resolver_reads_rotatable_secret_file(tmp_path):
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "provider-main").write_text("  exact-provider-secret\n", encoding="utf-8")
    resolver = MountedWebhookSecretResolver(secret_dir=secret_dir)

    assert await resolver("provider-main") == "exact-provider-secret"


@pytest.mark.asyncio
async def test_mounted_resolver_returns_none_for_missing_or_blank_file(tmp_path):
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    resolver = MountedWebhookSecretResolver(secret_dir=secret_dir)

    assert await resolver("provider-main") is None

    (secret_dir / "provider-main").write_text(" \n", encoding="utf-8")
    assert await resolver("provider-main") is None


@pytest.mark.asyncio
async def test_mounted_resolver_uses_an_isolated_file_per_provider(tmp_path):
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "provider-one").write_text("provider-one-secret", encoding="utf-8")
    (secret_dir / "provider-two").write_text("provider-two-secret", encoding="utf-8")
    resolver = MountedWebhookSecretResolver(secret_dir=secret_dir)

    assert await resolver("provider-one") == "provider-one-secret"
    assert await resolver("provider-two") == "provider-two-secret"
    assert await resolver("missing-provider") is None
    assert await resolver("../provider-one") is None


@pytest.mark.asyncio
async def test_mounted_resolver_rejects_oversized_or_invalid_secret_file(tmp_path):
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    secret_file = secret_dir / "provider-main"
    resolver = MountedWebhookSecretResolver(secret_dir=secret_dir, max_secret_bytes=8)

    secret_file.write_bytes(b"x" * 9)
    with pytest.raises(WebhookSecretResolutionError):
        await resolver("provider-main")

    secret_file.write_bytes(b"\xff")
    with pytest.raises(WebhookSecretResolutionError):
        await resolver("provider-main")


@pytest.mark.asyncio
async def test_runtime_service_verifies_mounted_secret_before_recording(tmp_path):
    secret = "mounted-secret"
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "provider-main").write_text(secret, encoding="utf-8")
    repository = _DeliveryRepository()
    service = create_webhook_processing_service(repository, secret_dir=secret_dir)
    body = json.dumps(
        {
            "action": "closed",
            "number": 17,
            "repository": {"full_name": "acme/platform"},
            "pull_request": {
                "number": 17,
                "merged": True,
                "merge_commit_sha": "merge-sha",
                "head": {"sha": "head-sha"},
                "base": {"sha": "base-sha"},
            },
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    result = await service.process(
        provider_id="provider-main",
        provider_type="gitea",
        headers={
            "X-Gitea-Event": "pull_request",
            "X-Gitea-Delivery": "delivery-17",
            "X-Gitea-Signature": signature,
        },
        body=body,
    )

    assert result.event is not None
    assert result.event.delivery_id == "delivery-17"
    assert len(repository.deliveries) == 1


@pytest.mark.asyncio
async def test_signature_for_one_provider_cannot_authenticate_another(tmp_path):
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "provider-one").write_text("provider-one-secret", encoding="utf-8")
    (secret_dir / "provider-two").write_text("provider-two-secret", encoding="utf-8")
    repository = _DeliveryRepository()
    service = create_webhook_processing_service(repository, secret_dir=secret_dir)
    body = json.dumps(
        {
            "action": "closed",
            "number": 17,
            "repository": {"full_name": "acme/platform"},
            "pull_request": {
                "number": 17,
                "merged": True,
                "merge_commit_sha": "merge-sha",
                "head": {"sha": "head-sha"},
                "base": {"sha": "base-sha"},
            },
        },
        separators=(",", ":"),
    ).encode()
    provider_one_signature = hmac.new(b"provider-one-secret", body, hashlib.sha256).hexdigest()

    with pytest.raises(WebhookProcessingError):
        await service.process(
            provider_id="provider-two",
            provider_type="gitea",
            headers={
                "X-Gitea-Event": "pull_request",
                "X-Gitea-Delivery": "delivery-18",
                "X-Gitea-Signature": provider_one_signature,
            },
            body=body,
        )

    assert repository.deliveries == {}
