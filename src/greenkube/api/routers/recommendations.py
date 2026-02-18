# src/greenkube/api/routers/recommendations.py
"""
API routes for optimization recommendations.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import get_carbon_repository, get_node_repository
from greenkube.core.config import config
from greenkube.core.recommender import Recommender
from greenkube.models.metrics import Recommendation
from greenkube.storage.base_repository import CarbonIntensityRepository, NodeRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/recommendations", response_model=List[Recommendation])
async def list_recommendations(
    namespace: Optional[str] = Query(None, description="Filter by Kubernetes namespace."),
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
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

    recommender = Recommender()
    return recommender.generate_recommendations(metrics, node_infos=node_infos)
