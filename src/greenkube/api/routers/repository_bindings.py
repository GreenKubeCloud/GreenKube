"""Repository binding API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.models.repository_binding import (
    RepositoryBinding,
    RepositoryBindingQuery,
)
from greenkube.storage.base_repository_binding_repository import RepositoryBindingRepository

router = APIRouter(prefix="/repository-bindings", tags=["repository-bindings"])


async def get_repository_binding_repository() -> RepositoryBindingRepository:
    """Resolve the configured repository without changing application composition."""
    from greenkube.core.config import get_config
    from greenkube.core.db import get_db_manager

    if get_config().DB_TYPE == "postgres":
        from greenkube.storage.postgres.repository_binding_repository import PostgresRepositoryBindingRepository

        return PostgresRepositoryBindingRepository(get_db_manager())
    from greenkube.storage.sqlite.repository_binding_repository import SQLiteRepositoryBindingRepository

    return SQLiteRepositoryBindingRepository(get_db_manager())


@router.get("", response_model=list[RepositoryBinding])
async def list_repository_bindings(
    cluster: str | None = Query(default=None),
    repository: RepositoryBindingRepository = Depends(get_repository_binding_repository),
) -> list[RepositoryBinding]:
    return await repository.list_bindings(cluster)


@router.get("/{namespace}/{workload_kind}/{workload_name}", response_model=RepositoryBinding)
async def get_repository_binding(
    namespace: str,
    workload_kind: str,
    workload_name: str,
    cluster: str = Query(default=""),
    repository: RepositoryBindingRepository = Depends(get_repository_binding_repository),
) -> RepositoryBinding:
    binding = await repository.get_binding(
        RepositoryBindingQuery(
            cluster=cluster,
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
        )
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="Repository binding not found")
    return binding


@router.post("", response_model=RepositoryBinding, status_code=201)
async def save_repository_binding(
    binding: RepositoryBinding,
    repository: RepositoryBindingRepository = Depends(get_repository_binding_repository),
) -> RepositoryBinding:
    return await repository.save_binding(binding)
