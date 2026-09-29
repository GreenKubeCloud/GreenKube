"""SQLite durable automation-operation queue."""

import json
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiosqlite import IntegrityError

from greenkube.storage.automation_operation_mapper import row_to_automation_operation
from greenkube.storage.base_automation_operation_repository import AutomationOperationRepository
from greenkube.utils.date_utils import to_iso_z


class SQLiteAutomationOperationRepository(AutomationOperationRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def enqueue(self, operation):
        now = to_iso_z(operation.created_at or datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            try:
                await conn.execute(
                    """INSERT INTO automation_operations
                    (recommendation_id, idempotency_key, fingerprint, request_json, actor, status,
                     attempts, available_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        operation.recommendation_id,
                        operation.idempotency_key,
                        operation.fingerprint,
                        json.dumps(operation.request, sort_keys=True),
                        operation.actor,
                        operation.status.value,
                        operation.attempts,
                        to_iso_z(operation.available_at) if operation.available_at else now,
                        now,
                        now,
                    ),
                )
            except IntegrityError:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM automation_operations WHERE idempotency_key = ?",
                        (operation.idempotency_key,),
                    )
                ).fetchone()
                if existing is None:
                    raise
                return row_to_automation_operation(existing)
            await conn.commit()
            cursor = await conn.execute(
                "SELECT * FROM automation_operations WHERE idempotency_key = ?", (operation.idempotency_key,)
            )
            return row_to_automation_operation(await cursor.fetchone())

    async def get_by_id(self, operation_id):
        return await self._get("id = ?", (operation_id,))

    async def get_by_idempotency_key(self, key):
        return await self._get("idempotency_key = ?", (key,))

    async def _get(self, clause, params):
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(f"SELECT * FROM automation_operations WHERE {clause}", params)
            row = await cursor.fetchone()
            return row_to_automation_operation(row) if row else None

    async def claim_next(self, worker_id, stale_after_seconds=900):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=stale_after_seconds)
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """SELECT * FROM automation_operations
                WHERE (status = 'queued' AND (available_at IS NULL OR available_at <= ?))
                   OR (status = 'running' AND locked_at < ?)
                ORDER BY created_at, id LIMIT 1""",
                (to_iso_z(now), to_iso_z(stale)),
            )
            row = await cursor.fetchone()
            if not row:
                await conn.commit()
                return None
            await conn.execute(
                """UPDATE automation_operations
                   SET status = 'running', locked_at = ?, updated_at = ?, attempts = attempts + 1
                   WHERE id = ?""",
                (to_iso_z(now), to_iso_z(now), row["id"]),
            )
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM automation_operations WHERE id = ?", (row["id"],))
            return row_to_automation_operation(await cursor.fetchone())

    async def mark_succeeded(self, operation_id: int, *, result: dict, preview_digest: str, commit_digest: str | None):
        return await self._finish(operation_id, "succeeded", result, preview_digest, commit_digest, None)

    async def mark_failed(self, operation_id: int, error: str):
        result = await self._finish(operation_id, "failed", None, None, None, error)
        if result is None:
            raise ValueError(f"Automation operation {operation_id} not found.")
        return result

    async def reschedule(self, operation_id, error, available_at):
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """UPDATE automation_operations
                   SET status = 'queued', available_at = ?, locked_at = NULL,
                       error = ?, updated_at = ? WHERE id = ?""",
                (to_iso_z(available_at), error, now, operation_id),
            )
            await conn.commit()
        result = await self.get_by_id(operation_id)
        if result is None:
            raise ValueError(f"Automation operation {operation_id} not found.")
        return result

    async def _finish(self, operation_id, status, result, preview_digest, commit_digest, error):
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            current = await (
                await conn.execute(
                    "SELECT preview_digest, commit_digest FROM automation_operations WHERE id = ?", (operation_id,)
                )
            ).fetchone()
            if current is None:
                raise ValueError(f"Automation operation {operation_id} not found.")
            if (current["preview_digest"] and preview_digest and current["preview_digest"] != preview_digest) or (
                current["commit_digest"] and commit_digest and current["commit_digest"] != commit_digest
            ):
                raise ValueError("Automation operation digests are immutable.")
            await conn.execute(
                """UPDATE automation_operations
                SET status = ?, result_json = ?, preview_digest = COALESCE(preview_digest, ?),
                    commit_digest = COALESCE(commit_digest, ?), error = ?, locked_at = NULL, updated_at = ?
                WHERE id = ?""",
                (
                    status,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    preview_digest,
                    commit_digest,
                    error,
                    now,
                    operation_id,
                ),
            )
            await conn.commit()
        result = await self.get_by_id(operation_id)
        if result is None:
            raise ValueError(f"Automation operation {operation_id} not found.")
        return result

    async def reconcile(self, stale_after_seconds=900):
        cutoff = to_iso_z(datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds))
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                "UPDATE automation_operations SET status = 'queued', locked_at = NULL, updated_at = ? "
                "WHERE status = 'running' AND locked_at < ?",
                (to_iso_z(datetime.now(timezone.utc)), cutoff),
            )
            await conn.commit()
            return cursor.rowcount
