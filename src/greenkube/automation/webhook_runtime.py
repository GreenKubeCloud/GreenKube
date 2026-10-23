"""Composition helpers for provider webhook verification in the API runtime."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

import aiofiles

from greenkube.automation.webhooks import MAX_WEBHOOK_BODY_BYTES, WebhookProcessingService
from greenkube.storage.base_gitops_repository import WebhookDeliveryRepository

DEFAULT_WEBHOOK_SECRET_DIR = Path("/etc/greenkube/webhooks")
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WebhookSecretResolutionError(RuntimeError):
    """A mounted provider webhook secret could not be read safely."""


class WebhookSecretResolver(Protocol):
    """Resolve the webhook key configured for one provider ID."""

    async def __call__(self, provider_id: str) -> str | None: ...


class MountedWebhookSecretResolver:
    """Read per-provider webhook keys from the API's read-only Secret mount."""

    def __init__(
        self,
        secret_file: str | Path | None = None,
        *,
        secret_dir: str | Path | None = None,
        max_secret_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    ) -> None:
        if isinstance(max_secret_bytes, bool) or not isinstance(max_secret_bytes, int):
            raise ValueError("max_secret_bytes must be an integer")
        if not 1 <= max_secret_bytes <= MAX_WEBHOOK_BODY_BYTES:
            raise ValueError(f"max_secret_bytes must be between 1 and {MAX_WEBHOOK_BODY_BYTES}")
        if secret_file is not None and secret_dir is not None:
            raise ValueError("configure either secret_file or secret_dir, not both")
        configured_path = (
            secret_dir
            if secret_dir is not None
            else secret_file
            if secret_file is not None
            else os.getenv("GITOPS_WEBHOOK_SECRET_DIR") or os.getenv("GITOPS_WEBHOOK_SECRET_FILE")
        )
        self.secret_dir = Path(configured_path) if configured_path else DEFAULT_WEBHOOK_SECRET_DIR
        self.max_secret_bytes = max_secret_bytes

    async def __call__(self, provider_id: str) -> str | None:
        """Read only the key mounted for this provider ID."""
        if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
            return None
        path = self._path_for_provider(provider_id)
        try:
            async with aiofiles.open(path, "rb") as secret_file:
                value = await secret_file.read(self.max_secret_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise WebhookSecretResolutionError("The mounted webhook Secret key file could not be read.") from None

        if len(value) > self.max_secret_bytes:
            raise WebhookSecretResolutionError("The mounted webhook Secret key file exceeds the supported size limit.")
        try:
            secret = value.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise WebhookSecretResolutionError("The mounted webhook Secret key file is not valid UTF-8.") from None
        return secret or None

    def _path_for_provider(self, provider_id: str) -> Path:
        root = self.secret_dir
        path = root / provider_id
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError):
            raise WebhookSecretResolutionError("The mounted webhook Secret path is invalid.") from None
        return path if path.is_absolute() else Path.cwd() / path


def create_webhook_processing_service(
    delivery_repository: WebhookDeliveryRepository,
    *,
    secret_file: str | Path | None = None,
    secret_dir: str | Path | None = None,
) -> WebhookProcessingService:
    """Build a signature-verifying service over durable delivery storage."""
    return WebhookProcessingService(
        secret_resolver=MountedWebhookSecretResolver(secret_file, secret_dir=secret_dir),
        delivery_repository=delivery_repository,
    )
