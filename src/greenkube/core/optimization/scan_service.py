# src/greenkube/core/optimization/scan_service.py
"""Controller-side recommendation scans (E1, BUG-21).

The controller runs :meth:`RecommendationScanService.tick` every
:data:`RECOMMENDATION_SCAN_POLL_INTERVAL` behind the controller Lease. Each tick:

1. fails runs left ``running`` by a crashed claimer (older than
   :data:`ABANDONED_RUN_AFTER`);
2. queues one cluster-wide ``scheduled`` run when none was requested within
   ``RECOMMENDATION_SCAN_INTERVAL`` (atomic in the database, so a second
   controller or a Lease hand-over cannot double-schedule);
3. claims and executes queued runs, including API requests
   (``POST /api/v1/optimization/runs``), oldest first.

The controller does not poll before its initial collection finished; it then
runs one forced tick (``tick(force=True)``) so the first scan always sees the
freshly collected data. Terminal runs older than :data:`RUN_RETENTION` are
pruned whenever a cluster-wide run is queued, together with the source runs
and source signals (roadmap E5) not seen for :data:`SOURCE_HISTORY_RETENTION`.

API processes never scan: they only enqueue. Two API replicas plus one
controller therefore produce exactly one scan per interval.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, List, Optional

from greenkube.models.optimization_run import OptimizationRun, OptimizationRunTrigger

if TYPE_CHECKING:
    from greenkube.core.optimization.engine import OptimizationEngine
    from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository
    from greenkube.storage.base_signal_repository import SignalRepository

logger = logging.getLogger(__name__)

#: How often the controller looks for queued and due runs.
RECOMMENDATION_SCAN_POLL_INTERVAL = "15s"
#: A ``running`` run older than this is considered abandoned by its claimer.
ABANDONED_RUN_AFTER = timedelta(minutes=30)
#: Upper bound on runs executed in one tick so other controller jobs keep their cadence.
MAX_RUNS_PER_TICK = 3
#: Succeeded and failed runs older than this are deleted.
RUN_RETENTION = timedelta(days=30)
#: Source runs started, and source signals last seen, before this are deleted.
SOURCE_HISTORY_RETENTION = timedelta(days=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_optimization_run_repository(db_manager=None) -> "OptimizationRunRepository":
    """Return the run repository for the configured backend."""
    from greenkube.core.config import get_config
    from greenkube.core.db import get_db_manager

    manager = db_manager if db_manager is not None else get_db_manager()
    if get_config().DB_TYPE == "postgres":
        from greenkube.storage.postgres.optimization_run_repository import PostgresOptimizationRunRepository

        return PostgresOptimizationRunRepository(manager)
    from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository

    return SQLiteOptimizationRunRepository(manager)


class RecommendationScanService:
    """Schedules, claims and executes optimization runs for one controller process."""

    def __init__(
        self,
        run_repository: "OptimizationRunRepository",
        *,
        combined_repo: "CombinedMetricsRepository",
        node_repo: "NodeRepository",
        reco_repo: "RecommendationRepository",
        interval: timedelta,
        identity: str,
        engine_factory: Optional[Callable[["OptimizationRunRepository"], "OptimizationEngine"]] = None,
        clock: Callable[[], datetime] = _utcnow,
        abandoned_after: timedelta = ABANDONED_RUN_AFTER,
        max_runs_per_tick: int = MAX_RUNS_PER_TICK,
        should_continue: Optional[Callable[[], bool]] = None,
        run_retention: timedelta = RUN_RETENTION,
        signal_repository: Optional["SignalRepository"] = None,
        source_history_retention: timedelta = SOURCE_HISTORY_RETENTION,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("The recommendation scan interval must be positive.")
        self.run_repository = run_repository
        self.combined_repo = combined_repo
        self.node_repo = node_repo
        self.reco_repo = reco_repo
        self.interval = interval
        self.identity = identity
        self.engine_factory = engine_factory or _default_engine
        self.clock = clock
        self.abandoned_after = abandoned_after
        self.max_runs_per_tick = max(1, int(max_runs_per_tick))
        self.should_continue = should_continue
        self.run_retention = run_retention
        self.signal_repository = signal_repository
        self.source_history_retention = source_history_retention

    async def tick(self, *, force: bool = False) -> List[OptimizationRun]:
        """Run one scheduling cycle and return the runs executed by this process.

        ``force`` queues a cluster-wide run even when the interval is already
        covered (deduplicated against a queued one). The controller forces its
        first tick after the initial collection so that scan uses fresh data.
        """
        now = self.clock()
        reaped = await self.run_repository.fail_abandoned(
            started_before=now - self.abandoned_after,
            completed_at=now,
            error="abandoned: the claiming controller stopped before completing the run",
        )
        if reaped:
            logger.warning("Marked %d abandoned optimization run(s) as failed.", reaped)

        scheduled: Optional[OptimizationRun]
        if force:
            scheduled, created = await self.run_repository.enqueue(
                namespace=None,
                trigger=OptimizationRunTrigger.SCHEDULED,
                requested_at=now,
                requested_by=self.identity,
            )
            if not created:
                scheduled = None
        else:
            scheduled = await self.run_repository.enqueue_if_due(
                requested_at=now,
                due_before=now - self.interval,
                requested_by=self.identity,
            )
        if scheduled is not None:
            logger.info("Queued scheduled optimization run %s.", scheduled.id)
            await self._prune(now)

        executed: List[OptimizationRun] = []
        for _ in range(self.max_runs_per_tick):
            if self.should_continue is not None and not self.should_continue():
                logger.info("Stopping recommendation scans: this controller no longer holds the Lease.")
                break
            run = await self.run_repository.claim_next(claimed_by=self.identity, started_at=self.clock())
            if run is None:
                break
            executed.append(await self._execute(run))
        return executed

    async def _prune(self, now: datetime) -> None:
        """Delete terminal runs past retention; failures never block scheduling."""
        try:
            pruned = await self.run_repository.prune_finished(completed_before=now - self.run_retention)
        except Exception as exc:
            logger.warning("Could not prune old optimization runs (%s).", type(exc).__name__)
            return
        if pruned:
            logger.info("Pruned %d optimization run(s) older than %s.", pruned, self.run_retention)
        if self.signal_repository is None:
            return
        try:
            pruned_sources = await self.signal_repository.prune(now - self.source_history_retention)
        except Exception as exc:
            logger.warning("Could not prune old source runs and signals (%s).", type(exc).__name__)
            return
        if pruned_sources:
            logger.info("Pruned %d source run and signal row(s).", pruned_sources)

    async def _execute(self, run: OptimizationRun) -> OptimizationRun:
        from greenkube.core.optimization.context_builder import RecommendationScanLimitError

        logger.info(
            "Starting optimization run %s (trigger=%s, namespace=%s).",
            run.id,
            run.trigger.value,
            run.namespace or "*",
        )
        engine = self.engine_factory(self.run_repository)
        try:
            result = await engine.run_scan(
                self.combined_repo,
                self.node_repo,
                self.reco_repo,
                namespace=run.namespace,
                run=run,
            )
        except RecommendationScanLimitError as exc:
            logger.error("Optimization run %s exceeded its metric-row bound: %s", run.id, exc)
        except Exception:
            logger.exception("Optimization run %s failed.", run.id)
        else:
            finished = result.run or run
            logger.info(
                "Optimization run %s %s: %d recommendation(s), +%s new, %s changed, %s resolved.",
                run.id,
                finished.status.value,
                finished.recommendation_count,
                finished.new_count,
                finished.changed_count,
                finished.resolved_count,
            )
            return finished
        if run.id is not None:
            stored = await self.run_repository.get(run.id)
            if stored is not None:
                return stored
        return run


def _default_engine(run_repository: "OptimizationRunRepository") -> "OptimizationEngine":
    from greenkube.core.optimization.engine import OptimizationEngine
    from greenkube.storage.base_signal_repository import create_signal_repository

    return OptimizationEngine(run_repository=run_repository, signal_repository=create_signal_repository())


def create_recommendation_scan_service(
    *,
    should_continue: Optional[Callable[[], bool]] = None,
    identity: Optional[str] = None,
) -> RecommendationScanService:
    """Compose the scan service from the process configuration and factories."""
    from greenkube.core.config import get_config
    from greenkube.core.factory import (
        get_combined_metrics_repository,
        get_node_repository,
        get_recommendation_repository,
    )
    from greenkube.core.leader_election import process_identity
    from greenkube.storage.base_signal_repository import create_signal_repository

    cfg = get_config()
    return RecommendationScanService(
        build_optimization_run_repository(),
        combined_repo=get_combined_metrics_repository(),
        node_repo=get_node_repository(),
        reco_repo=get_recommendation_repository(),
        interval=cfg.recommendation_scan_interval,
        identity=identity or process_identity(),
        should_continue=should_continue,
        signal_repository=create_signal_repository(),
    )
