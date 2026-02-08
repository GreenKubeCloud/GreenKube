# src/greenkube/api/routers/namespaces.py
"""
API route for listing available Kubernetes namespaces.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends

from greenkube.api.dependencies import get_carbon_repository
from greenkube.storage.base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/namespaces", response_model=List[str])
async def list_namespaces(
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
):
    """Return a sorted list of unique namespaces seen in recent metrics."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)
    namespaces = sorted({m.namespace for m in metrics})
    return namespaces
