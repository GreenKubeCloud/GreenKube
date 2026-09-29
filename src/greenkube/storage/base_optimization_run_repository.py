"""Repository contract for optimization execution records."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from greenkube.models.optimization_run import OptimizationRun


class OptimizationRunRepository(ABC):
    """Persists optimization run state and its completeness metadata."""

    @abstractmethod
    async def create(self, run: OptimizationRun) -> OptimizationRun:
        pass

    @abstractmethod
    async def complete(
        self,
        run_id: int,
        *,
        status: str,
        completed_at: datetime,
        recommendation_count: int,
        analyzer_count: int,
        failed_analyzer_count: int,
        error: Optional[str] = None,
    ) -> OptimizationRun:
        pass

    @abstractmethod
    async def get(self, run_id: int) -> Optional[OptimizationRun]:
        pass

    @abstractmethod
    async def latest(self, namespace: Optional[str] = None) -> Optional[OptimizationRun]:
        pass
