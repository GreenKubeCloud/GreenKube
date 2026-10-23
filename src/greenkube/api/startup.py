# src/greenkube/api/startup.py
"""
One-shot startup tasks executed after the DB connection is established.

These tasks run as fire-and-forget background coroutines so they never
block application startup. Each task must catch its own exceptions; a
failure must not prevent the API from serving requests.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.core.config import get_config
from greenkube.core.db import get_db_manager
from greenkube.core.factory import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.core.optimization.context_builder import RecommendationScanLimitError
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.gitops.contracts import Plan

logger = logging.getLogger(__name__)
PLAN_HISTORY_RETENTION_DAYS = 90


class DuePlanExpirer(Protocol):
    async def expire_due_plans(self) -> Sequence[Plan]: ...

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]: ...

    async def reconcile_exhausted_operations(self) -> Sequence[Plan]: ...


async def run_plan_expiry_loop(plan_service: DuePlanExpirer, *, interval_seconds: float = 60.0) -> None:
    """Keep unapproved plans from remaining actionable beyond their TTL."""
    if interval_seconds < 1:
        raise ValueError("plan expiry interval must be at least one second")
    last_pruned_at: datetime | None = None
    while True:
        now = datetime.now(UTC)
        reconcile = getattr(plan_service, "reconcile_exhausted_operations", None)
        if callable(reconcile):
            try:
                reconciled = await cast(Any, reconcile)()
            except Exception as exc:
                logger.warning("Plan operation reconciliation failed (non-fatal): %s", type(exc).__name__)
            else:
                if reconciled:
                    logger.warning("Reconciled %d applying plans after exhausted operations.", len(reconciled))
        try:
            expired = await plan_service.expire_due_plans()
        except Exception as exc:
            logger.warning("Plan expiry sweep failed (non-fatal): %s", type(exc).__name__)
        else:
            if expired:
                logger.info("Plan expiry sweep marked %d plans stale.", len(expired))
        if last_pruned_at is None or now - last_pruned_at >= timedelta(days=1):
            try:
                pruned = await plan_service.prune_retained_history(
                    cutoff=now - timedelta(days=PLAN_HISTORY_RETENTION_DAYS)
                )
            except Exception as exc:
                logger.warning("Automation history retention failed (non-fatal): %s", type(exc).__name__)
            else:
                if any(pruned.values()):
                    logger.info("Automation history retention pruned %s.", pruned)
                last_pruned_at = now
        await asyncio.sleep(interval_seconds)


async def run_startup_recommendation_scan() -> None:
    """Pre-populate the recommendations DB immediately after startup.

    With ephemeral storage (``persistence.enabled=false``) the SQLite database is
    empty after every pod restart.  Without this scan, ``greenkube_top_recommendations``
    emits no gauge values until the first manual API call, causing a gap in
    Prometheus data after deployments or node restarts.  With persistent storage
    (PostgreSQL) the existing data is refreshed so stale records are reconciled
    against the current cluster state.

    The scan is a best-effort operation:
    - If Prometheus or OpenCost is transiently unavailable at boot time, the scan
      logs a warning and exits without crashing the API.
    - If no metrics exist yet in the DB (brand-new install), the scan reconciles
      the empty set and waits for the background scheduler to populate metrics.
    """
    try:
        combined_repo = get_combined_metrics_repository()
        node_repo = get_node_repository()
        reco_repo = get_recommendation_repository()

        run_repo = None
        if get_config().DB_TYPE == "sqlite":
            from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository

            run_repo = SQLiteOptimizationRunRepository(get_db_manager())
        else:
            from greenkube.storage.postgres.optimization_run_repository import PostgresOptimizationRunRepository

            run_repo = PostgresOptimizationRunRepository(get_db_manager())
        engine = OptimizationEngine(run_repository=run_repo)
        try:
            recommendations = await engine.refresh(combined_repo, node_repo, reco_repo, namespace=None)
        except RecommendationScanLimitError as exc:
            logger.error("Startup recommendation scan exceeded its metric-row bound: %s", exc)
            return

        update_recommendation_metrics(recommendations)

        logger.info("Startup recommendation scan complete: %d recommendations persisted.", len(recommendations))
    except Exception as exc:
        logger.warning("Startup recommendation scan failed (non-fatal): %s", exc)
