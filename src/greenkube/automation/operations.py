"""Durable automation operation values and integrity helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class AutomationOperationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AutomationOperation:
    id: Optional[int]
    recommendation_id: int
    idempotency_key: str
    fingerprint: str
    request: dict[str, Any]
    actor: str = "user"
    status: AutomationOperationStatus = AutomationOperationStatus.QUEUED
    preview_digest: Optional[str] = None
    commit_digest: Optional[str] = None
    attempts: int = 0
    available_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def digest(value: Any) -> str:
    """Return a stable SHA-256 digest for JSON-compatible values."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: (
            item.model_dump(mode="json")
            if hasattr(item, "model_dump")
            else item.value
            if isinstance(item, Enum)
            else item.isoformat()
            if isinstance(item, datetime)
            else str(item)
        ),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def operation_fingerprint(recommendation: Any, request: dict[str, Any]) -> str:
    """Fingerprint the immutable recommendation input and requested operation."""
    return digest(
        {
            "recommendation_id": recommendation.id,
            "recommendation_status": getattr(recommendation.status, "value", recommendation.status),
            "recommendation_type": getattr(recommendation.type, "value", recommendation.type),
            "patch": recommendation.patch,
            "owner_kind": recommendation.owner_kind,
            "owner_name": recommendation.owner_name,
            "request": request,
        }
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
