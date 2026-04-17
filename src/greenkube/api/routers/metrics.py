# src/greenkube/api/routers/metrics.py
"""
API routes for carbon/cost/energy metrics.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.api.dependencies import get_combined_metrics_repository, validate_namespace
from greenkube.api.schemas import (
    MetricsSummaryResponse,
    NamespaceBreakdownItem,
    PaginatedMetricsResponse,
    TimeseriesPoint,
    TopPodItem,
)
from greenkube.storage.base_repository import CombinedMetricsRepository
from greenkube.utils.date_utils import parse_duration

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_time_range(last: Optional[str]) -> tuple[datetime, datetime]:
    """Compute (start, end) time range. Defaults to last 24h."""
    end = datetime.now(timezone.utc)
    if last:
        try:
            delta = parse_duration(last)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        start = end - delta
    else:
        start = end - timedelta(days=1)
    return start, end


@router.get("/metrics", response_model=PaginatedMetricsResponse)
async def list_metrics(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    offset: int = Query(0, ge=0, description="Number of records to skip."),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum number of records to return."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """List combined metrics for the given time range and optional namespace filter."""
    start, end = _get_time_range(last)
    metrics = await repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=namespace)
    total = len(metrics)
    page = metrics[offset : offset + limit]
    return PaginatedMetricsResponse(total=total, offset=offset, limit=limit, items=page)


@router.get("/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return an aggregated summary of metrics over the time range."""
    start, end = _get_time_range(last)
    # Use SQL-level aggregation when available (e.g. SQLite) to avoid loading
    # all rows into Python objects — typically 10–20x faster for demo mode.
    summary = await repo.aggregate_summary(start_time=start, end_time=end, namespace=namespace)
    scope2 = summary.get("total_co2e_grams", 0.0)
    scope3 = summary.get("total_embodied_co2e_grams", 0.0)
    return MetricsSummaryResponse(**summary, total_co2e_all_scopes=scope2 + scope3)


_GRANULARITY_FORMATS = {
    "hour": "%Y-%m-%dT%H:00:00Z",
    "day": "%Y-%m-%dT00:00:00Z",
    "week": "%Y-W%V",
    "month": "%Y-%m-01T00:00:00Z",
}


@router.get("/metrics/timeseries", response_model=List[TimeseriesPoint])
async def metrics_timeseries(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    granularity: Optional[str] = Query("hour", description="Grouping: 'hour', 'day', 'week', 'month'."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return time-series data aggregated by the specified granularity."""
    if granularity not in _GRANULARITY_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid granularity '{granularity}'. Use: {', '.join(_GRANULARITY_FORMATS.keys())}.",
        )

    start, end = _get_time_range(last)
    # Use SQL-level aggregation when available (e.g. SQLite) to avoid loading
    # all rows into Python objects — typically 10–20x faster for demo mode.
    rows = await repo.aggregate_timeseries(start_time=start, end_time=end, granularity=granularity, namespace=namespace)
    return [
        TimeseriesPoint(
            timestamp=row["timestamp"],
            co2e_grams=row["co2e_grams"],
            embodied_co2e_grams=row["embodied_co2e_grams"],
            total_co2e_all_scopes=row["co2e_grams"] + row.get("embodied_co2e_grams", 0.0),
            total_cost=row["total_cost"],
            joules=row["energy_joules"],
            pod_count=0,  # not aggregated at timeseries level
            namespace_count=0,  # not aggregated at timeseries level
        )
        for row in rows
    ]


@router.get("/metrics/by-namespace", response_model=List[NamespaceBreakdownItem])
async def metrics_by_namespace(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return metrics aggregated by namespace (lightweight, SQL-level)."""
    start, end = _get_time_range(last)
    rows = await repo.aggregate_by_namespace(start_time=start, end_time=end, namespace=namespace)
    return [NamespaceBreakdownItem(**row) for row in rows]


@router.get("/metrics/top-pods", response_model=List[TopPodItem])
async def metrics_top_pods(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d')."),
    limit: int = Query(10, ge=1, le=50, description="Number of top pods to return."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return top pods by CO2 emissions (lightweight, SQL-level)."""
    start, end = _get_time_range(last)
    rows = await repo.aggregate_top_pods(start_time=start, end_time=end, namespace=namespace, limit=limit)
    return [TopPodItem(**row) for row in rows]
