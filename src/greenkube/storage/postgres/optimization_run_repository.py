"""PostgreSQL optimization run repository."""

from greenkube.models.optimization_run import OptimizationRun
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository


def _run(row) -> OptimizationRun:
    return OptimizationRun(**dict(row))


class PostgresOptimizationRunRepository(OptimizationRunRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def create(self, run):
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """INSERT INTO optimization_runs
                (status, namespace, started_at, completed_at, recommendation_count,
                 analyzer_count, failed_analyzer_count, error)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
                run.status.value,
                run.namespace,
                run.started_at,
                run.completed_at,
                run.recommendation_count,
                run.analyzer_count,
                run.failed_analyzer_count,
                run.error,
            )
        return _run(row)

    async def complete(
        self, run_id, *, status, completed_at, recommendation_count, analyzer_count, failed_analyzer_count, error=None
    ):
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """UPDATE optimization_runs SET status=$1, completed_at=$2, recommendation_count=$3,
                analyzer_count=$4, failed_analyzer_count=$5, error=$6 WHERE id=$7 RETURNING *""",
                status,
                completed_at,
                recommendation_count,
                analyzer_count,
                failed_analyzer_count,
                error,
                run_id,
            )
        if row is None:
            raise ValueError(f"Optimization run {run_id} not found.")
        return _run(row)

    async def get(self, run_id):
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow("SELECT * FROM optimization_runs WHERE id=$1", run_id)
        return _run(row) if row else None

    async def latest(self, namespace=None):
        query = "SELECT * FROM optimization_runs"
        params = ()
        if namespace is not None:
            query += " WHERE namespace = $1"
            params = (namespace,)
        query += " ORDER BY started_at DESC, id DESC LIMIT 1"
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(query, *params)
        return _run(row) if row else None
