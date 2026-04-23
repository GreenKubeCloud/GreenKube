# src/greenkube/api/routers/recommendations.py
"""
API routes for the recommendation lifecycle.

Endpoints:
  GET  /recommendations            - Live recommendations (runs recommender, upserts DB)
  GET  /recommendations/active     - Current active recommendations from DB
  GET  /recommendations/ignored    - All permanently ignored recommendations
  GET  /recommendations/history    - Historical records filtered by time range
  GET  /recommendations/savings    - Aggregate CO2 and cost savings from applied recs
  PATCH /recommendations/{id}/apply  - Mark a recommendation as applied
  PATCH /recommendations/{id}/ignore - Permanently ignore a recommendation
  DELETE /recommendations/{id}/ignore - Un-ignore a recommendation (restore to active)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    validate_namespace,
)
from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.collectors.hpa_collector import HPACollector
from greenkube.core.config import get_config
from greenkube.core.recommender import Recommender
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    Recommendation,
    RecommendationRecord,
    RecommendationSavingsSummary,
)
from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/recommendations", response_model=List[Recommendation])
async def list_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Analyze recent metrics, upsert recommendations in DB, and return them.

    Recommended CPU and memory values are guaranteed to be at least the
    configured minimums (RECOMMENDATION_MIN_CPU_MILLICORES /
    RECOMMENDATION_MIN_MEMORY_BYTES), so all returned recommendations are
    actionable as-is.
    """
    lookback_days = get_config().RECOMMENDATION_LOOKBACK_DAYS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    metrics = await repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=namespace)

    if not metrics:
        return []

    node_infos = []
    try:
        node_infos = await node_repo.get_latest_snapshots_before(end)
    except Exception as e:
        logger.warning("Could not fetch node snapshots for recommendations: %s", e)

    hpa_targets = None
    try:
        hpa_collector = HPACollector()
        hpa_targets = await hpa_collector.collect()
    except Exception as e:
        logger.warning("Could not collect HPA targets: %s. Proceeding without HPA filtering.", e)

    recommender = Recommender()
    recommendations = recommender.generate_recommendations(
        metrics,
        node_infos=node_infos,
        hpa_targets=hpa_targets,
    )

    update_recommendation_metrics(recommendations)

    try:
        records = [RecommendationRecord.from_recommendation(r) for r in recommendations]
        if records:
            await reco_repo.upsert_recommendations(records)
    except Exception as e:
        logger.error("Failed to upsert recommendation history: %s", e)

    return recommendations


@router.get("/recommendations/active", response_model=List[RecommendationRecord])
async def list_active_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return currently active recommendations from the database.

    This endpoint reads directly from the DB without re-running the recommender engine,
    making it faster for the dashboard to poll.
    """
    return await reco_repo.get_active_recommendations(namespace=namespace)


@router.get("/recommendations/ignored", response_model=List[RecommendationRecord])
async def list_ignored_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return all permanently ignored recommendations.

    Useful for reviewing ignored recommendations and deciding to un-ignore them.
    """
    return await reco_repo.get_ignored_recommendations(namespace=namespace)


@router.get("/recommendations/history", response_model=List[RecommendationRecord])
async def list_recommendation_history(
    start: str = Query(..., description="Start datetime (ISO 8601)."),
    end: str = Query(..., description="End datetime (ISO 8601)."),
    type: Optional[str] = Query(None, description="Filter by recommendation type."),
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Retrieve all recommendation records within a time range (any status)."""
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

    return await reco_repo.get_recommendations(
        start=start_dt,
        end=end_dt,
        rec_type=type,
        namespace=namespace,
    )


@router.get("/recommendations/applied", response_model=List[RecommendationRecord])
async def list_applied_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return all applied recommendations, ordered by most recently applied.

    Used by the Realized Savings section to show the details of each
    implemented optimization.
    """
    return await reco_repo.get_applied_recommendations(namespace=namespace)


@router.get("/recommendations/savings", response_model=RecommendationSavingsSummary)
async def get_savings_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return aggregate CO2e and cost savings from all applied recommendations.

    This is GreenKube's core value metric: the total environmental and financial
    impact of the optimizations that have been implemented.
    """
    return await reco_repo.get_savings_summary(namespace=namespace)


@router.patch("/recommendations/{rec_id}/apply", response_model=RecommendationRecord)
async def apply_recommendation(
    rec_id: int,
    request: ApplyRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Mark a recommendation as applied.

    Optionally provide the actual values applied (CPU/memory). If actual savings
    are not provided, the estimated potential savings are used as a conservative proxy.

    A recommendation is considered applied even when the actual value deviates from
    the recommendation (e.g., reducing CPU to 50m instead of the suggested 40m).
    """
    try:
        return await reco_repo.apply_recommendation(rec_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/recommendations/{rec_id}/ignore", response_model=RecommendationRecord)
async def ignore_recommendation(
    rec_id: int,
    request: IgnoreRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Permanently ignore a recommendation.

    The recommendation will no longer appear in the active list. It remains visible
    under GET /recommendations/ignored so it can be reviewed or un-ignored later.

    Typical use case: a pod cannot support HPA due to a RWO PVC, or a namespace
    intentionally runs at low utilization (e.g., a staging environment).
    """
    try:
        return await reco_repo.ignore_recommendation(rec_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/recommendations/{rec_id}/ignore", response_model=RecommendationRecord)
async def unignore_recommendation(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Restore an ignored recommendation back to active status.

    Useful when circumstances change (e.g., the PVC is migrated to RWX) or when a
    recommendation was accidentally ignored.
    """
    try:
        return await reco_repo.unignore_recommendation(rec_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
