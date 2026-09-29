"""SQLite repository for repository bindings."""

import json
from datetime import datetime, timezone

from greenkube.models.repository_binding import (
    BindingSource,
    BindingStatus,
    RepositoryBinding,
    RepositoryBindingQuery,
)
from greenkube.storage.base_repository_binding_repository import RepositoryBindingRepository
from greenkube.utils.date_utils import to_iso_z


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
        evidence=json.loads(row["evidence"] or "{}"),
        discovered_at=datetime.fromisoformat(row["discovered_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
    )


class SQLiteRepositoryBindingRepository(RepositoryBindingRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def save_binding(self, binding: RepositoryBinding) -> RepositoryBinding:
        now = to_iso_z(datetime.now(timezone.utc))
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                """
                INSERT INTO repository_bindings
                    (cluster, namespace, workload_kind, workload_name, repo_url, path, branch,
                     source, priority, status, confidence, evidence, discovered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster, namespace, workload_kind, workload_name, source, repo_url, path)
                DO UPDATE SET branch=excluded.branch, priority=excluded.priority, status=excluded.status,
                    confidence=excluded.confidence, evidence=excluded.evidence, updated_at=excluded.updated_at
                """,
                (
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
                    to_iso_z(binding.discovered_at),
                    now,
                ),
            )
            await conn.commit()
            row = await (
                await conn.execute(
                    """
                    SELECT * FROM repository_bindings
                    WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                      AND source = ? AND repo_url = ? AND path IS ?
                    """,
                    (
                        binding.cluster,
                        binding.namespace,
                        binding.workload_kind,
                        binding.workload_name,
                        binding.source.value,
                        binding.repo_url,
                        binding.path,
                    ),
                )
            ).fetchone()
            return _binding(row)

    async def get_binding(self, query: RepositoryBindingQuery) -> RepositoryBinding | None:
        async with self.db_manager.connection_scope() as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT * FROM repository_bindings
                    WHERE cluster = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                    ORDER BY priority DESC, updated_at DESC LIMIT 1
                    """,
                    (query.cluster, query.namespace, query.workload_kind, query.workload_name),
                )
            ).fetchone()
            return _binding(row) if row else None

    async def list_bindings(self, cluster: str | None = None) -> list[RepositoryBinding]:
        async with self.db_manager.connection_scope() as conn:
            query, params = "SELECT * FROM repository_bindings", []
            if cluster is not None:
                query += " WHERE cluster = ?"
                params.append(cluster)
            query += " ORDER BY namespace, workload_kind, workload_name, priority DESC"
            rows = await (await conn.execute(query, params)).fetchall()
            return [_binding(row) for row in rows]

    async def delete_binding(self, binding_id: int) -> bool:
        async with self.db_manager.connection_scope() as conn:
            cursor = await conn.execute("DELETE FROM repository_bindings WHERE id = ?", (binding_id,))
            await conn.commit()
            return cursor.rowcount > 0
