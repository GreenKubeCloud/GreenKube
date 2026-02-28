# src/greenkube/api/routers/recommendations.py
"""
API routes for optimization recommendations.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import (
    get_carbon_repository,
    get_node_repository,
    get_recommendation_repository,
    validate_namespace,
)
from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.collectors.hpa_collector import HPACollector
from greenkube.core.config import config
from greenkube.core.recommender import Recommender
from greenkube.models.metrics import Recommendation, RecommendationRecord
from greenkube.storage.base_repository import CarbonIntensityRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/recommendations", response_model=List[Recommendation])
async def list_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Analyze recent metrics and return optimization recommendations."""
    lookback_days = config.RECOMMENDATION_LOOKBACK_DAYS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)

    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]

    if not metrics:
        return []

    # Fetch node info for node-level recommendations
    node_infos = []
    try:
        node_infos = await node_repo.get_latest_snapshots_before(end)
    except Exception as e:
        logger.warning("Could not fetch node snapshots for recommendations: %s", e)

    # Detect existing HPAs to avoid redundant autoscaling recommendations
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

    # Update Prometheus metrics
    update_recommendation_metrics(recommendations)

    # Persist recommendations in history (best-effort)
    try:
        records = [RecommendationRecord.from_recommendation(r) for r in recommendations]
        await reco_repo.save_recommendations(records)
    except Exception as e:
        logger.error("Failed to save recommendation history: %s", e)

    return recommendations


@router.get("/recommendations/history", response_model=List[RecommendationRecord])
async def list_recommendation_history(
    start: str = Query(..., description="Start datetime (ISO 8601)."),
    end: str = Query(..., description="End datetime (ISO 8601)."),
    type: Optional[str] = Query(None, description="Filter by recommendation type."),
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Retrieve historical recommendation records."""
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

    records = await reco_repo.get_recommendations(
        start=start_dt,
        end=end_dt,
        rec_type=type,
        namespace=namespace,
    )
    return records
