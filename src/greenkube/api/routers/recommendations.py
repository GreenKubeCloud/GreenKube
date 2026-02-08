# src/greenkube/api/routers/recommendations.py
"""
API routes for optimization recommendations.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from greenkube.api.dependencies import get_carbon_repository
from greenkube.core.recommender import Recommender
from greenkube.models.metrics import Recommendation
from greenkube.storage.base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/recommendations", response_model=List[Recommendation])
async def list_recommendations(
    namespace: Optional[str] = Query(None, description="Filter by Kubernetes namespace."),
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
):
    """Analyze recent metrics and return optimization recommendations."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)

    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]

    if not metrics:
        return []

    recommender = Recommender()
    zombie_recs = recommender.generate_zombie_recommendations(metrics)
    rightsizing_recs = recommender.generate_rightsizing_recommendations(metrics)
    return zombie_recs + rightsizing_recs
