# src/greenkube/api/startup.py
"""
One-shot startup tasks executed after the DB connection is established.

These tasks run as fire-and-forget background coroutines so they never
block application startup. Each task must catch its own exceptions; a
failure must not prevent the API from serving requests.
"""

import logging

from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.core.factory import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.core.optimization.engine import OptimizationEngine

logger = logging.getLogger(__name__)


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

        engine = OptimizationEngine()
        recommendations = await engine.refresh(combined_repo, node_repo, reco_repo, namespace=None)

        update_recommendation_metrics(recommendations)

        logger.info("Startup recommendation scan complete: %d recommendations persisted.", len(recommendations))
    except Exception as exc:
        logger.warning("Startup recommendation scan failed (non-fatal): %s", exc)
