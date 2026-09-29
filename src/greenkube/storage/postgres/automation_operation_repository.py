"""PostgreSQL durable automation-operation queue."""

import json
from datetime import datetime, timedelta, timezone

import asyncpg

from greenkube.storage.automation_operation_mapper import row_to_automation_operation
from greenkube.storage.base_automation_operation_repository import AutomationOperationRepository


class PostgresAutomationOperationRepository(AutomationOperationRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def enqueue(self, operation):
        now = operation.created_at or datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            try:
                row = await conn.fetchrow(
                    """INSERT INTO automation_operations
                    (recommendation_id, idempotency_key, fingerprint, request_json, actor, status,
                     attempts, available_at, created_at, updated_at)
                    VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$9) RETURNING *""",
                    operation.recommendation_id,
                    operation.idempotency_key,
                    operation.fingerprint,
                    json.dumps(operation.request, sort_keys=True),
                    operation.actor,
                    operation.status.value,
                    operation.attempts,
                    operation.available_at or now,
                    now,
                )
            except asyncpg.UniqueViolationError:
                row = await conn.fetchrow(
                    "SELECT * FROM automation_operations WHERE idempotency_key = $1", operation.idempotency_key
                )
                if row is None:
                    raise
            return row_to_automation_operation(row)

    async def get_by_id(self, operation_id):
        return await self._get("id = $1", operation_id)

    async def get_by_idempotency_key(self, key):
        return await self._get("idempotency_key = $1", key)

    async def _get(self, clause, value):
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(f"SELECT * FROM automation_operations WHERE {clause}", value)
            return row_to_automation_operation(row) if row else None

    async def claim_next(self, worker_id, stale_after_seconds=900):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=stale_after_seconds)
        async with self.db_manager.connection_scope() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT * FROM automation_operations
                    WHERE (status = 'queued' AND (available_at IS NULL OR available_at <= $1))
                       OR (status = 'running' AND locked_at < $2)
                    ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                    now,
                    stale,
                )
                if not row:
                    return None
                row = await conn.fetchrow(
                    """UPDATE automation_operations SET status='running', locked_at=$1,
                       updated_at=$1, attempts=attempts+1 WHERE id=$2 RETURNING *""",
                    now,
                    row["id"],
                )
                return row_to_automation_operation(row)

    async def mark_succeeded(self, operation_id: int, *, result: dict, preview_digest: str, commit_digest: str | None):
        return await self._finish(operation_id, "succeeded", result, preview_digest, commit_digest, None)

    async def mark_failed(self, operation_id: int, error: str):
        return await self._finish(operation_id, "failed", None, None, None, error)

    async def reschedule(self, operation_id, error, available_at):
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """UPDATE automation_operations
                   SET status='queued', available_at=$1, locked_at=NULL,
                       error=$2, updated_at=NOW() WHERE id=$3 RETURNING *""",
                available_at,
                error,
                operation_id,
            )
            if not row:
                raise ValueError(f"Automation operation {operation_id} not found.")
            return row_to_automation_operation(row)

    async def _finish(self, operation_id, status, result, preview_digest, commit_digest, error):
        async with self.db_manager.connection_scope() as conn:
            current = await conn.fetchrow(
                "SELECT preview_digest, commit_digest FROM automation_operations WHERE id=$1", operation_id
            )
            if not current:
                raise ValueError(f"Automation operation {operation_id} not found.")
            if (current["preview_digest"] and preview_digest and current["preview_digest"] != preview_digest) or (
                current["commit_digest"] and commit_digest and current["commit_digest"] != commit_digest
            ):
                raise ValueError("Automation operation digests are immutable.")
            row = await conn.fetchrow(
                """UPDATE automation_operations SET status=$1, result_json=$2::jsonb,
                preview_digest=COALESCE(preview_digest,$3), commit_digest=COALESCE(commit_digest,$4),
                error=$5, locked_at=NULL, updated_at=NOW() WHERE id=$6 RETURNING *""",
                status,
                json.dumps(result) if result is not None else None,
                preview_digest,
                commit_digest,
                error,
                operation_id,
            )
            if not row:
                raise ValueError(f"Automation operation {operation_id} not found.")
            return row_to_automation_operation(row)

    async def reconcile(self, stale_after_seconds=900):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                "UPDATE automation_operations SET status='queued', locked_at=NULL, updated_at=NOW() "
                "WHERE status='running' AND locked_at < $1",
                cutoff,
            )
            return int(result.split()[-1])
