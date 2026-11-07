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

API processes never scan: they only enqueue. Two API replicas plus one
controller therefore produce exactly one scan per interval.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, List, Optional

from greenkube.models.optimization_run import OptimizationRun

if TYPE_CHECKING:
    from greenkube.core.optimization.engine import OptimizationEngine
    from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
    from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)

#: How often the controller looks for queued and due runs.
RECOMMENDATION_SCAN_POLL_INTERVAL = "15s"
#: A ``running`` run older than this is considered abandoned by its claimer.
ABANDONED_RUN_AFTER = timedelta(minutes=30)
#: Upper bound on runs executed in one tick so other controller jobs keep their cadence.
MAX_RUNS_PER_TICK = 3


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

    async def tick(self) -> List[OptimizationRun]:
        """Run one scheduling cycle and return the runs executed by this process."""
        now = self.clock()
        reaped = await self.run_repository.fail_abandoned(
            started_before=now - self.abandoned_after,
            completed_at=now,
            error="abandoned: the claiming controller stopped before completing the run",
        )
        if reaped:
            logger.warning("Marked %d abandoned optimization run(s) as failed.", reaped)

        scheduled = await self.run_repository.enqueue_if_due(
            requested_at=now,
            due_before=now - self.interval,
            requested_by=self.identity,
        )
        if scheduled is not None:
            logger.info("Queued scheduled optimization run %s.", scheduled.id)

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

    return OptimizationEngine(run_repository=run_repository)


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

    cfg = get_config()
    return RecommendationScanService(
        build_optimization_run_repository(),
        combined_repo=get_combined_metrics_repository(),
        node_repo=get_node_repository(),
        reco_repo=get_recommendation_repository(),
        interval=cfg.recommendation_scan_interval,
        identity=identity or process_identity(),
        should_continue=should_continue,
    )
