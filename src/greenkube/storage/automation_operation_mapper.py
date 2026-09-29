"""Database mapping for automation operations."""

import json
from datetime import datetime, timezone

from greenkube.automation.operations import AutomationOperation, AutomationOperationStatus


def _get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def row_to_automation_operation(row) -> AutomationOperation:
    request_value = _get(row, "request_json")
    if isinstance(request_value, str):
        request_value = json.loads(request_value)
    if not isinstance(request_value, dict):
        request_value = {}
    attempts_value = _get(row, "attempts", 0)
    return AutomationOperation(
        id=_get(row, "id"),
        recommendation_id=int(row["recommendation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        fingerprint=str(row["fingerprint"]),
        request=request_value,
        actor=str(_get(row, "actor", "user")),
        status=AutomationOperationStatus(_get(row, "status", "queued")),
        preview_digest=_get(row, "preview_digest"),
        commit_digest=_get(row, "commit_digest"),
        attempts=int(attempts_value or 0),
        available_at=_datetime(_get(row, "available_at")),
        locked_at=_datetime(_get(row, "locked_at")),
        error=_get(row, "error"),
        result=(
            json.loads(row["result_json"]) if isinstance(_get(row, "result_json"), str) else _get(row, "result_json")
        ),
        created_at=_datetime(_get(row, "created_at")) or datetime.now(timezone.utc),
        updated_at=_datetime(_get(row, "updated_at")),
    )
