"""SQLite optimization run repository."""

from datetime import datetime

import aiosqlite

from greenkube.models.optimization_run import OptimizationRun
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
from greenkube.utils.date_utils import to_iso_z


def _run(row: aiosqlite.Row) -> OptimizationRun:
    return OptimizationRun(
        id=row["id"],
        status=row["status"],
        namespace=row["namespace"],
        started_at=datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")),
        completed_at=(
            datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")) if row["completed_at"] else None
        ),
        recommendation_count=row["recommendation_count"],
        analyzer_count=row["analyzer_count"],
        failed_analyzer_count=row["failed_analyzer_count"],
        error=row["error"],
    )


class SQLiteOptimizationRunRepository(OptimizationRunRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def create(self, run: OptimizationRun) -> OptimizationRun:
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute(
                """INSERT INTO optimization_runs
                (status, namespace, started_at, completed_at, recommendation_count,
                 analyzer_count, failed_analyzer_count, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.status.value,
                    run.namespace,
                    to_iso_z(run.started_at),
                    to_iso_z(run.completed_at) if run.completed_at else None,
                    run.recommendation_count,
                    run.analyzer_count,
                    run.failed_analyzer_count,
                    run.error,
                ),
            )
            await conn.commit()
            run.id = cursor.lastrowid
            return run

    async def complete(
        self, run_id, *, status, completed_at, recommendation_count, analyzer_count, failed_analyzer_count, error=None
    ):
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """UPDATE optimization_runs SET status=?, completed_at=?, recommendation_count=?,
                analyzer_count=?, failed_analyzer_count=?, error=? WHERE id=?""",
                (
                    status,
                    to_iso_z(completed_at),
                    recommendation_count,
                    analyzer_count,
                    failed_analyzer_count,
                    error,
                    run_id,
                ),
            )
            await conn.commit()
        result = await self.get(run_id)
        if result is None:
            raise ValueError(f"Optimization run {run_id} not found.")
        return result

    async def get(self, run_id):
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM optimization_runs WHERE id=?", (run_id,))
            row = await cursor.fetchone()
        return _run(row) if row else None

    async def latest(self, namespace=None):
        query = "SELECT * FROM optimization_runs"
        params = ()
        if namespace is not None:
            query += " WHERE namespace = ?"
            params = (namespace,)
        query += " ORDER BY started_at DESC, id DESC LIMIT 1"
        async with self.db_manager.connection_scope() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return _run(row) if row else None
