"""Database mapping for durable plan operations."""

from datetime import datetime, timezone
from typing import Any

from greenkube.gitops.contracts import Operation, OperationStatus
from greenkube.storage.gitops_mapper import datetime_value


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def row_to_plan_operation(row: Any) -> Operation:
    return Operation(
        operation_id=str(_get(row, "operation_id", "")),
        plan_id=str(_get(row, "plan_id", "")),
        idempotency_key=str(_get(row, "idempotency_key", "")),
        status=OperationStatus(_get(row, "status", OperationStatus.QUEUED.value)),
        attempts=int(_get(row, "attempts", 0) or 0),
        available_at=datetime_value(_get(row, "available_at")),
        locked_at=datetime_value(_get(row, "locked_at")),
        worker_id=_get(row, "worker_id"),
        last_error=_get(row, "last_error"),
        provider_id=_get(row, "provider_id"),
        commit_sha=_get(row, "commit_sha"),
        pull_request_number=(
            int(_get(row, "pull_request_number")) if _get(row, "pull_request_number") is not None else None
        ),
        created_at=datetime_value(_get(row, "created_at")) or datetime.now(timezone.utc),
        updated_at=datetime_value(_get(row, "updated_at")),
    )
