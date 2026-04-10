# src/greenkube/api/routers/namespaces.py
"""
API route for listing available Kubernetes namespaces.

Uses the namespace_cache table when available to avoid scanning
the entire combined_metrics table.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends

from greenkube.api.dependencies import get_combined_metrics_repository
from greenkube.storage.base_repository import CombinedMetricsRepository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/namespaces", response_model=List[str])
async def list_namespaces(
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return a sorted list of unique namespaces seen in recent metrics."""
    return await repo.list_namespaces()
