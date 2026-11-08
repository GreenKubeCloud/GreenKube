"""Repository contract for optimization execution records and the run request queue."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunDiff,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
)

#: Upper bound for ``list_runs``.
MAX_RUN_LIST_LIMIT = 100


class OptimizationRunRepository(ABC):
    """Persists optimization runs, queued run requests and their outcome.

    Queue invariants shared by every backend:

    * at most one ``queued`` run exists per scope (cluster-wide or one namespace);
      a pending cluster-wide run also satisfies namespaced requests;
    * ``claim_next`` hands each queued run to exactly one caller, even across
      processes, because the claim is a single conditional UPDATE;
    * ``enqueue_if_due`` creates at most one cluster-wide run per interval;
    * ``finish`` only updates a run that is still pending: a run already failed
      by ``fail_abandoned`` keeps its terminal state.
    """

    @abstractmethod
    async def create(self, run: OptimizationRun) -> OptimizationRun:
        """Insert a run as-is (used for direct, non-queued executions)."""

    @abstractmethod
    async def finish(self, run_id: int, completion: OptimizationRunCompletion) -> OptimizationRun:
        """Write the terminal state of a pending run and return the stored row.

        A run that is already terminal is left unchanged and returned as stored.
        """

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
        new_count: Optional[int] = None,
        changed_count: Optional[int] = None,
        resolved_count: Optional[int] = None,
        unchanged_count: Optional[int] = None,
        source_outcomes: Optional[Iterable[SourceRunOutcome]] = None,
        diff: Optional[OptimizationRunDiff] = None,
    ) -> OptimizationRun:
        """Backward-compatible keyword form of :meth:`finish`."""
        return await self.finish(
            run_id,
            OptimizationRunCompletion(
                status=OptimizationRunStatus(status),
                completed_at=completed_at,
                recommendation_count=recommendation_count,
                analyzer_count=analyzer_count,
                failed_analyzer_count=failed_analyzer_count,
                new_count=new_count,
                changed_count=changed_count,
                resolved_count=resolved_count,
                unchanged_count=unchanged_count,
                source_outcomes=list(source_outcomes or []),
                diff=diff,
                error=error,
            ),
        )

    @abstractmethod
    async def get(self, run_id: int) -> Optional[OptimizationRun]:
        pass

    @abstractmethod
    async def latest(self, namespace: Optional[str] = None) -> Optional[OptimizationRun]:
        pass

    @abstractmethod
    async def list_runs(self, limit: int = 20) -> List[OptimizationRun]:
        """Most recently requested runs first (any status)."""

    @abstractmethod
    async def enqueue(
        self,
        *,
        namespace: Optional[str],
        trigger: OptimizationRunTrigger,
        requested_at: datetime,
        requested_by: Optional[str] = None,
    ) -> Tuple[OptimizationRun, bool]:
        """Queue a run request; returns ``(run, created)``.

        When a queued run already covers the scope, that run is returned with
        ``created=False`` instead of inserting a duplicate.
        """

    @abstractmethod
    async def enqueue_if_due(
        self,
        *,
        requested_at: datetime,
        due_before: datetime,
        requested_by: Optional[str] = None,
    ) -> Optional[OptimizationRun]:
        """Atomically queue a scheduled cluster-wide run when none is due yet.

        A run is created only when no cluster-wide run is queued or running and
        none was requested after ``due_before``. Returns the created run, or
        ``None`` when the current interval is already covered.
        """

    @abstractmethod
    async def claim_next(self, *, claimed_by: str, started_at: datetime) -> Optional[OptimizationRun]:
        """Move the oldest queued run to ``running`` for this caller, or return ``None``."""

    @abstractmethod
    async def fail_abandoned(self, *, started_before: datetime, completed_at: datetime, error: str) -> int:
        """Fail ``running`` runs started before ``started_before`` (crashed or fenced-out claimers)."""

    async def find_recent_covering(
        self,
        *,
        namespace: Optional[str],
        completed_after: datetime,
    ) -> Optional[OptimizationRun]:
        """Newest run whose scope covers ``namespace`` and that is pending or recently finished.

        Matches runs that are ``queued`` or ``running``, or that completed at or
        after ``completed_after``. A cluster-wide run covers every namespace.
        Backends without support return ``None`` (no reuse).
        """
        return None

    async def prune_finished(self, *, completed_before: datetime) -> int:
        """Delete succeeded and failed runs completed before ``completed_before``; returns the count."""
        return 0


def bounded_run_limit(limit: int) -> int:
    """Clamp a list limit to ``1..MAX_RUN_LIST_LIMIT``."""
    return max(1, min(int(limit), MAX_RUN_LIST_LIMIT))
