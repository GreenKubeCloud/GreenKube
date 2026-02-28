# src/greenkube/storage/base_embodied_repository.py
"""Abstract interface for embodied-carbon profile repositories."""

import abc
from typing import Optional


class BaseEmbodiedRepository(abc.ABC):
    """Repository for managing embodied carbon profiles (Scope 3)."""

    @abc.abstractmethod
    async def get_profile(self, provider: str, instance_type: str) -> Optional[dict]:
        """Retrieve the embodied carbon profile for a given provider and instance type."""

    @abc.abstractmethod
    async def save_profile(
        self,
        provider: str,
        instance_type: str,
        gwp: float,
        lifespan: int,
        source: str = "boavizta_api",
    ):
        """Save or upsert an embodied carbon profile."""
