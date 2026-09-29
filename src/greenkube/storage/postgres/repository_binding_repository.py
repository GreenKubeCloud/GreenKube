"""PostgreSQL repository for repository bindings."""

import json

from greenkube.models.repository_binding import BindingSource, BindingStatus, RepositoryBinding, RepositoryBindingQuery
from greenkube.storage.base_repository_binding_repository import RepositoryBindingRepository


def _binding(row) -> RepositoryBinding:
    return RepositoryBinding(
        id=str(row["id"]),
        cluster=row["cluster"],
        namespace=row["namespace"],
        workload_kind=row["workload_kind"],
        workload_name=row["workload_name"],
        repo_url=row["repo_url"],
        path=row["path"],
        branch=row["branch"],
        source=BindingSource(row["source"]),
        priority=row["priority"],
        status=BindingStatus(row["status"]),
        confidence=row["confidence"],
        evidence=row["evidence"] if isinstance(row["evidence"], dict) else json.loads(row["evidence"] or "{}"),
        discovered_at=row["discovered_at"],
        updated_at=row["updated_at"],
    )


class PostgresRepositoryBindingRepository(RepositoryBindingRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_binding(self, binding: RepositoryBinding) -> RepositoryBinding:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO repository_bindings
                    (cluster, namespace, workload_kind, workload_name, repo_url, path, branch,
                     source, priority, status, confidence, evidence, discovered_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (cluster, namespace, workload_kind, workload_name, source, repo_url, path)
                DO UPDATE SET branch=excluded.branch, priority=excluded.priority, status=excluded.status,
                    confidence=excluded.confidence, evidence=excluded.evidence, updated_at=excluded.updated_at
                RETURNING *
                """,
                binding.cluster,
                binding.namespace,
                binding.workload_kind,
                binding.workload_name,
                binding.repo_url,
                binding.path,
                binding.branch,
                binding.source.value,
                binding.priority,
                binding.status.value,
                binding.confidence,
                json.dumps(binding.evidence),
                binding.discovered_at,
                binding.updated_at,
            )
            return _binding(row)

    async def get_binding(self, query: RepositoryBindingQuery) -> RepositoryBinding | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM repository_bindings
                WHERE cluster=$1 AND namespace=$2 AND workload_kind=$3 AND workload_name=$4
                ORDER BY priority DESC, updated_at DESC LIMIT 1
                """,
                query.cluster,
                query.namespace,
                query.workload_kind,
                query.workload_name,
            )
            return _binding(row) if row else None

    async def list_bindings(self, cluster: str | None = None) -> list[RepositoryBinding]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                "SELECT * FROM repository_bindings"
                + (" WHERE cluster=$1" if cluster is not None else "")
                + " ORDER BY namespace, workload_kind, workload_name, priority DESC",
                *([cluster] if cluster is not None else []),
            )
            return [_binding(row) for row in rows]

    async def delete_binding(self, binding_id: int) -> bool:
        async with self.db_manager.connection_scope() as conn:
            result = await conn.execute("DELETE FROM repository_bindings WHERE id=$1", binding_id)
            return result.endswith(" 1")
