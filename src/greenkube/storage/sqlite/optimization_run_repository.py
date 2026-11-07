"""SQLite optimization run repository and run request queue.

Every queue transition is a single SQL statement so it stays atomic across
processes sharing the database file (API replicas and the controller).
"""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import aiosqlite

from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunDiff,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
)
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository, bounded_run_limit

_INSERT_COLUMNS = (
    "status, namespace, trigger_type, requested_by, requested_at, started_at, completed_at, claimed_by, "
    "recommendation_count, analyzer_count, failed_analyzer_count, error"
)


def _ts(value: Optional[datetime]) -> Optional[str]:
    """Fixed-width UTC timestamp so string comparison matches time order."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _json_list(value: Any) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> Optional[dict]:
    if not value:
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _trigger(value: Any) -> OptimizationRunTrigger:
    try:
        return OptimizationRunTrigger(value)
    except ValueError:
        return OptimizationRunTrigger.LEGACY


def _run(row: aiosqlite.Row) -> OptimizationRun:
    keys = set(row.keys())

    def get(name: str) -> Any:
        return row[name] if name in keys else None

    diff = _json_dict(get("diff"))
    return OptimizationRun(
        id=row["id"],
        status=row["status"],
        namespace=row["namespace"],
        trigger=_trigger(get("trigger_type")),
        requested_by=get("requested_by"),
        requested_at=_parse_ts(get("requested_at")),
        started_at=_parse_ts(row["started_at"]),
        completed_at=_parse_ts(row["completed_at"]),
        claimed_by=get("claimed_by"),
        recommendation_count=row["recommendation_count"],
        analyzer_count=row["analyzer_count"],
        failed_analyzer_count=row["failed_analyzer_count"],
        new_count=get("new_count"),
        changed_count=get("changed_count"),
        resolved_count=get("resolved_count"),
        unchanged_count=get("unchanged_count"),
        source_outcomes=[SourceRunOutcome.model_validate(item) for item in _json_list(get("source_outcomes"))],
        diff=OptimizationRunDiff.model_validate(diff) if diff is not None else None,
        error=row["error"],
    )


class SQLiteOptimizationRunRepository(OptimizationRunRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def _fetch_one(self, query: str, params: tuple) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return _run(row) if row else None

    async def create(self, run: OptimizationRun) -> OptimizationRun:
        requested_at = run.requested_at or run.started_at or datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                f"INSERT INTO optimization_runs ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.status.value,
                    run.namespace,
                    run.trigger.value,
                    run.requested_by,
                    _ts(requested_at),
                    _ts(run.started_at),
                    _ts(run.completed_at),
                    run.claimed_by,
                    run.recommendation_count,
                    run.analyzer_count,
                    run.failed_analyzer_count,
                    run.error,
                ),
            )
            await conn.commit()
            run_id = cursor.lastrowid
        return run.model_copy(update={"id": run_id, "requested_at": requested_at})

    async def finish(self, run_id: int, completion: OptimizationRunCompletion) -> OptimizationRun:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """UPDATE optimization_runs SET status=?, completed_at=?, recommendation_count=?,
                analyzer_count=?, failed_analyzer_count=?, new_count=?, changed_count=?, resolved_count=?,
                unchanged_count=?, source_outcomes=?, diff=?, error=? WHERE id=?""",
                (
                    completion.status.value,
                    _ts(completion.completed_at),
                    completion.recommendation_count,
                    completion.analyzer_count,
                    completion.failed_analyzer_count,
                    completion.new_count,
                    completion.changed_count,
                    completion.resolved_count,
                    completion.unchanged_count,
                    json.dumps([outcome.model_dump(mode="json") for outcome in completion.source_outcomes]),
                    json.dumps(completion.diff.model_dump(mode="json")) if completion.diff is not None else None,
                    completion.error,
                    run_id,
                ),
            )
            await conn.commit()
        result = await self.get(run_id)
        if result is None:
            raise ValueError(f"Optimization run {run_id} not found.")
        return result

    async def get(self, run_id: int) -> Optional[OptimizationRun]:
        return await self._fetch_one("SELECT * FROM optimization_runs WHERE id=?", (run_id,))

    async def latest(self, namespace: Optional[str] = None) -> Optional[OptimizationRun]:
        query = "SELECT * FROM optimization_runs"
        params: tuple = ()
        if namespace is not None:
            query += " WHERE namespace = ?"
            params = (namespace,)
        query += " ORDER BY requested_at DESC, id DESC LIMIT 1"
        return await self._fetch_one(query, params)

    async def list_runs(self, limit: int = 20) -> List[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM optimization_runs ORDER BY requested_at DESC, id DESC LIMIT ?",
                (bounded_run_limit(limit),),
            )
            rows = await cursor.fetchall()
        return [_run(row) for row in rows]

    async def enqueue(
        self,
        *,
        namespace: Optional[str],
        trigger: OptimizationRunTrigger,
        requested_at: datetime,
        requested_by: Optional[str] = None,
    ) -> Tuple[OptimizationRun, bool]:
        # A claim can race with the lookup below; retry so the caller always
        # receives either its own row or the queued row that covers its scope.
        for _ in range(5):
            existing = None
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute(
                    f"""INSERT INTO optimization_runs ({_INSERT_COLUMNS})
                    SELECT 'queued', ?, ?, ?, ?, NULL, NULL, NULL, 0, 0, 0, NULL
                    WHERE NOT EXISTS (
                        SELECT 1 FROM optimization_runs WHERE status = 'queued' AND namespace IS NULL
                    )
                    ON CONFLICT DO NOTHING""",
                    (namespace, trigger.value, requested_by, _ts(requested_at)),
                )
                created_id = cursor.lastrowid if cursor.rowcount == 1 else None
                if created_id is None:
                    cursor = await conn.execute(
                        """SELECT * FROM optimization_runs
                        WHERE status = 'queued' AND (namespace IS NULL OR namespace IS ?)
                        ORDER BY (namespace IS NULL) DESC, id ASC LIMIT 1""",
                        (namespace,),
                    )
                    existing = await cursor.fetchone()
                await conn.commit()
            if created_id is not None:
                created = await self.get(created_id)
                if created is not None:
                    return created, True
            elif existing is not None:
                return _run(existing), False
        raise RuntimeError("Could not enqueue an optimization run after repeated claim races.")

    async def enqueue_if_due(
        self,
        *,
        requested_at: datetime,
        due_before: datetime,
        requested_by: Optional[str] = None,
    ) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                f"""INSERT INTO optimization_runs ({_INSERT_COLUMNS})
                SELECT 'queued', NULL, ?, ?, ?, NULL, NULL, NULL, 0, 0, 0, NULL
                WHERE NOT EXISTS (
                    SELECT 1 FROM optimization_runs
                    WHERE namespace IS NULL
                      AND (status IN ('queued', 'running') OR requested_at > ?)
                )
                ON CONFLICT DO NOTHING""",
                (
                    OptimizationRunTrigger.SCHEDULED.value,
                    requested_by,
                    _ts(requested_at),
                    _ts(due_before),
                ),
            )
            created_id = cursor.lastrowid if cursor.rowcount == 1 else None
            await conn.commit()
        return await self.get(created_id) if created_id is not None else None

    async def claim_next(self, *, claimed_by: str, started_at: datetime) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """UPDATE optimization_runs SET status = 'running', started_at = ?, claimed_by = ?
                WHERE status = 'queued' AND id = (
                    SELECT id FROM optimization_runs WHERE status = 'queued'
                    ORDER BY requested_at ASC, id ASC LIMIT 1
                )
                RETURNING *""",
                (_ts(started_at), claimed_by),
            )
            row = await cursor.fetchone()
            await conn.commit()
        return _run(row) if row else None

    async def fail_abandoned(self, *, started_before: datetime, completed_at: datetime, error: str) -> int:
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """UPDATE optimization_runs SET status = ?, completed_at = ?, error = COALESCE(error, ?)
                WHERE status = 'running' AND (started_at IS NULL OR started_at < ?)""",
                (OptimizationRunStatus.FAILED.value, _ts(completed_at), error, _ts(started_before)),
            )
            count = cursor.rowcount
            await conn.commit()
        return max(int(count or 0), 0)
