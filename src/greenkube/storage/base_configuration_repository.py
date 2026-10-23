"""Persistence contract for versioned runtime configuration overrides."""

from abc import ABC, abstractmethod


class BaseConfigurationRepository(ABC):
    """Store only mutable runtime overrides, never bootstrap or secret values."""

    @abstractmethod
    async def load(self) -> tuple[int, dict[str, str]]:
        """Return the latest version and its runtime overrides."""

    @abstractmethod
    async def save(self, values: dict[str, str], version: int) -> None:
        """Atomically replace runtime overrides at *version*."""

    @abstractmethod
    async def clear(self, version: int) -> None:
        """Atomically remove runtime overrides at *version*."""
