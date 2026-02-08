# src/greenkube/api/routers/metrics.py
"""
API routes for carbon/cost/energy metrics.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.api.dependencies import get_carbon_repository
from greenkube.api.schemas import MetricsSummaryResponse
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_repository import CarbonIntensityRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_last(last: str) -> timedelta:
    """Parse a duration string like '10min', '2h', '7d' into a timedelta.

    Raises:
        ValueError: If the format is invalid.
    """
    match = re.match(r"^(\d+)(min|[hdwmy])$", last.lower())
    if not match:
        raise ValueError(f"Invalid format for 'last': '{last}'. Use '10min', '2h', '7d', '3w', '1m' (month), '1y'.")
    value, unit = int(match.group(1)), match.group(2)
    mapping = {
        "min": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
        "w": timedelta(weeks=value),
        "m": timedelta(days=value * 30),
        "y": timedelta(days=value * 365),
    }
    return mapping[unit]


def _get_time_range(last: Optional[str]) -> tuple[datetime, datetime]:
    """Compute (start, end) time range. Defaults to last 24h."""
    end = datetime.now(timezone.utc)
    if last:
        try:
            delta = _parse_last(last)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        start = end - delta
    else:
        start = end - timedelta(days=1)
    return start, end


@router.get("/metrics", response_model=List[CombinedMetric])
async def list_metrics(
    namespace: Optional[str] = Query(None, description="Filter by Kubernetes namespace."),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
):
    """List combined metrics for the given time range and optional namespace filter."""
    start, end = _get_time_range(last)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)
    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]
    return metrics


@router.get("/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(
    namespace: Optional[str] = Query(None, description="Filter by Kubernetes namespace."),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    repo: CarbonIntensityRepository = Depends(get_carbon_repository),
):
    """Return an aggregated summary of metrics over the time range."""
    start, end = _get_time_range(last)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)
    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]

    total_co2 = sum(m.co2e_grams for m in metrics)
    total_embodied = sum(m.embodied_co2e_grams or 0.0 for m in metrics)
    total_cost = sum(m.total_cost for m in metrics)
    total_energy = sum(m.joules for m in metrics)
    pods = {m.pod_name for m in metrics}
    namespaces = {m.namespace for m in metrics}

    return MetricsSummaryResponse(
        total_co2e_grams=total_co2,
        total_embodied_co2e_grams=total_embodied,
        total_cost=total_cost,
        total_energy_joules=total_energy,
        pod_count=len(pods),
        namespace_count=len(namespaces),
    )
