from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseExporter(ABC):
    """Abstract base class for file exporters.

    Subclasses should provide a DEFAULT_FILENAME and implement `export`.
    """

    DEFAULT_FILENAME: str = "greenkube-report"

    @abstractmethod
    async def export(self, data: List[Dict[str, Any]], path: str | None = None) -> str:
        """Export the provided data to disk. Return the written path."""
        raise NotImplementedError()
