# src/greenkube/api/startup.py
"""
One-shot startup tasks executed after the DB connection is established.

These tasks run as fire-and-forget background coroutines so they never
block application startup. Each task must catch its own exceptions; a
failure must not prevent the API from serving requests.
"""

import logging
from datetime import datetime, timedelta, timezone

from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.api.routers.recommendations import _get_active_k8s_namespaces
from greenkube.collectors.hpa_collector import HPACollector
from greenkube.core.config import get_config
from greenkube.core.factory import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.core.recommender import Recommender
from greenkube.models.metrics import RecommendationRecord

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
    - If no metrics exist yet in the DB (brand-new install), the scan skips
      silently and waits for the background scheduler to populate them.
    """
    try:
        cfg = get_config()
        lookback_days = cfg.RECOMMENDATION_LOOKBACK_DAYS
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        analysis_window_seconds = (end - start).total_seconds()

        combined_repo = get_combined_metrics_repository()
        node_repo = get_node_repository()
        reco_repo = get_recommendation_repository()

        metrics = await combined_repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=None)

        active_namespaces = await _get_active_k8s_namespaces()
        if active_namespaces is not None and metrics:
            metrics = [m for m in metrics if m.namespace in active_namespaces]

        if not metrics:
            logger.info("Startup recommendation scan: no metrics in DB yet, skipping.")
            return

        node_infos = []
        try:
            node_infos = await node_repo.get_latest_snapshots_before(end)
        except Exception as exc:
            logger.warning("Startup scan: could not fetch node snapshots: %s", exc)

        hpa_targets = None
        try:
            hpa_collector = HPACollector()
            hpa_targets = await hpa_collector.collect()
        except Exception as exc:
            logger.warning("Startup scan: could not collect HPA targets: %s", exc)

        recommender = Recommender()
        recommendations = recommender.generate_recommendations(
            metrics,
            node_infos=node_infos,
            hpa_targets=hpa_targets,
            analysis_window_seconds=analysis_window_seconds,
        )

        update_recommendation_metrics(recommendations)

        records = [RecommendationRecord.from_recommendation(r) for r in recommendations]
        if records:
            await reco_repo.upsert_recommendations(records)
        await reco_repo.reconcile_active_recommendations(records, namespace=None)

        logger.info("Startup recommendation scan complete: %d recommendations persisted.", len(records))
    except Exception as exc:
        logger.warning("Startup recommendation scan failed (non-fatal): %s", exc)
