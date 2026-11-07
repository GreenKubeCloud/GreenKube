"""PostgreSQL optimization run repository and run request queue.

Every queue transition is a single SQL statement: the pending-scope unique
index deduplicates concurrent requests and ``FOR UPDATE SKIP LOCKED`` hands a
queued run to exactly one claimer. Scheduling (``enqueue_if_due``) also holds a
transaction-scoped advisory lock, so a run claimed between two controllers'
checks cannot let both of them schedule the same interval.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunDiff,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
)
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository, bounded_run_limit

logger = logging.getLogger(__name__)

#: Advisory lock serializing scheduled-run creation ("GKSCAN" in ASCII).
_SCHEDULE_LOCK_KEY = 0x474B5343414E

_INSERT_COLUMNS = (
    "status, namespace, trigger_type, requested_by, requested_at, started_at, completed_at, claimed_by, "
    "recommendation_count, analyzer_count, failed_analyzer_count, error"
)


def _json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def _trigger(value: Any) -> OptimizationRunTrigger:
    try:
        return OptimizationRunTrigger(value)
    except ValueError:
        return OptimizationRunTrigger.LEGACY


def _run(row) -> OptimizationRun:
    data = dict(row)
    outcomes = _json(data.get("source_outcomes"))
    diff = _json(data.get("diff"))
    return OptimizationRun(
        id=data["id"],
        status=data["status"],
        namespace=data.get("namespace"),
        trigger=_trigger(data.get("trigger_type")),
        requested_by=data.get("requested_by"),
        requested_at=data.get("requested_at"),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        claimed_by=data.get("claimed_by"),
        recommendation_count=data.get("recommendation_count") or 0,
        analyzer_count=data.get("analyzer_count") or 0,
        failed_analyzer_count=data.get("failed_analyzer_count") or 0,
        new_count=data.get("new_count"),
        changed_count=data.get("changed_count"),
        resolved_count=data.get("resolved_count"),
        unchanged_count=data.get("unchanged_count"),
        source_outcomes=[SourceRunOutcome.model_validate(item) for item in outcomes]
        if isinstance(outcomes, list)
        else [],
        diff=OptimizationRunDiff.model_validate(diff) if isinstance(diff, dict) else None,
        error=data.get("error"),
    )


class PostgresOptimizationRunRepository(OptimizationRunRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def create(self, run: OptimizationRun) -> OptimizationRun:
        requested_at = run.requested_at or run.started_at or datetime.now(timezone.utc)
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO optimization_runs ({_INSERT_COLUMNS})
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
                run.status.value,
                run.namespace,
                run.trigger.value,
                run.requested_by,
                requested_at,
                run.started_at,
                run.completed_at,
                run.claimed_by,
                run.recommendation_count,
                run.analyzer_count,
                run.failed_analyzer_count,
                run.error,
            )
        return _run(row)

    async def finish(self, run_id: int, completion: OptimizationRunCompletion) -> OptimizationRun:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """UPDATE optimization_runs SET status=$1, completed_at=$2, recommendation_count=$3,
                analyzer_count=$4, failed_analyzer_count=$5, new_count=$6, changed_count=$7, resolved_count=$8,
                unchanged_count=$9, source_outcomes=$10::jsonb, diff=$11::jsonb, error=$12
                WHERE id=$13 AND status IN ('queued', 'running') RETURNING *""",
                completion.status.value,
                completion.completed_at,
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
            )
        if row is not None:
            return _run(row)
        stored = await self.get(run_id)
        if stored is None:
            raise ValueError(f"Optimization run {run_id} not found.")
        logger.warning(
            "Optimization run %s was already %s; its outcome was not overwritten.", run_id, stored.status.value
        )
        return stored

    async def get(self, run_id: int) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM optimization_runs WHERE id=$1", run_id)
        return _run(row) if row else None

    async def latest(self, namespace: Optional[str] = None) -> Optional[OptimizationRun]:
        query = "SELECT * FROM optimization_runs"
        params: tuple = ()
        if namespace is not None:
            query += " WHERE namespace = $1"
            params = (namespace,)
        query += " ORDER BY requested_at DESC, id DESC LIMIT 1"
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(query, *params)
        return _run(row) if row else None

    async def list_runs(self, limit: int = 20) -> List[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_runs ORDER BY requested_at DESC, id DESC LIMIT $1",
                bounded_run_limit(limit),
            )
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
            async with self.db_manager.connection_scope() as conn:
                row = await conn.fetchrow(
                    f"""INSERT INTO optimization_runs ({_INSERT_COLUMNS})
                    SELECT 'queued', $1::text, $2::text, $3::text, $4::timestamptz,
                        NULL::timestamptz, NULL::timestamptz, NULL::text, 0, 0, 0, NULL::text
                    WHERE NOT EXISTS (
                        SELECT 1 FROM optimization_runs WHERE status = 'queued' AND namespace IS NULL
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING *""",
                    namespace,
                    trigger.value,
                    requested_by,
                    requested_at,
                )
                if row is not None:
                    return _run(row), True
                existing = await conn.fetchrow(
                    """SELECT * FROM optimization_runs
                    WHERE status = 'queued' AND (namespace IS NULL OR namespace IS NOT DISTINCT FROM $1::text)
                    ORDER BY (namespace IS NULL) DESC, id ASC LIMIT 1""",
                    namespace,
                )
            if existing is not None:
                return _run(existing), False
        raise RuntimeError("Could not enqueue an optimization run after repeated claim races.")

    async def enqueue_if_due(
        self,
        *,
        requested_at: datetime,
        due_before: datetime,
        requested_by: Optional[str] = None,
    ) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn, conn.transaction():
            # Taken before the INSERT so its snapshot sees any run another
            # controller scheduled (and possibly already claimed) meanwhile.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULE_LOCK_KEY)
            row = await conn.fetchrow(
                f"""INSERT INTO optimization_runs ({_INSERT_COLUMNS})
                SELECT 'queued', NULL::text, $1::text, $2::text, $3::timestamptz,
                    NULL::timestamptz, NULL::timestamptz, NULL::text, 0, 0, 0, NULL::text
                WHERE NOT EXISTS (
                    SELECT 1 FROM optimization_runs
                    WHERE namespace IS NULL
                      AND (status IN ('queued', 'running') OR requested_at > $4::timestamptz)
                )
                ON CONFLICT DO NOTHING
                RETURNING *""",
                OptimizationRunTrigger.SCHEDULED.value,
                requested_by,
                requested_at,
                due_before,
            )
        return _run(row) if row else None

    async def claim_next(self, *, claimed_by: str, started_at: datetime) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """UPDATE optimization_runs SET status = 'running', started_at = $1, claimed_by = $2
                WHERE id = (
                    SELECT id FROM optimization_runs WHERE status = 'queued'
                    ORDER BY requested_at ASC, id ASC
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                RETURNING *""",
                started_at,
                claimed_by,
            )
        return _run(row) if row else None

    async def fail_abandoned(self, *, started_before: datetime, completed_at: datetime, error: str) -> int:
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                """UPDATE optimization_runs SET status = $1, completed_at = $2, error = COALESCE(error, $3)
                WHERE status = 'running' AND (started_at IS NULL OR started_at < $4)""",
                OptimizationRunStatus.FAILED.value,
                completed_at,
                error,
                started_before,
            )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0

    async def find_recent_covering(
        self,
        *,
        namespace: Optional[str],
        completed_after: datetime,
    ) -> Optional[OptimizationRun]:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM optimization_runs
                WHERE (namespace IS NULL OR namespace IS NOT DISTINCT FROM $1::text)
                  AND (status IN ('queued', 'running') OR completed_at >= $2::timestamptz)
                ORDER BY requested_at DESC, id DESC LIMIT 1""",
                namespace,
                completed_after,
            )
        return _run(row) if row else None

    async def prune_finished(self, *, completed_before: datetime) -> int:
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute(
                """DELETE FROM optimization_runs
                WHERE status IN ('succeeded', 'failed') AND completed_at < $1::timestamptz""",
                completed_before,
            )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0
