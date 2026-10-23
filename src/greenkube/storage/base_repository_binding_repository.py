"""Persistence port for repository bindings."""

from abc import ABC, abstractmethod

from greenkube.models.repository_binding import RepositoryBinding, RepositoryBindingQuery


class RepositoryBindingRepository(ABC):
    """Store and retrieve resolved repository bindings."""

    @abstractmethod
    async def save_binding(self, binding: RepositoryBinding) -> RepositoryBinding: ...

    @abstractmethod
    async def get_binding(self, query: RepositoryBindingQuery) -> RepositoryBinding | None: ...

    @abstractmethod
    async def list_bindings(self, cluster: str | None = None) -> list[RepositoryBinding]: ...

    @abstractmethod
    async def delete_binding(self, binding_id: int) -> bool: ...
