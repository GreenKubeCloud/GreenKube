# src/greenkube/api/routers/metrics.py
"""
API routes for carbon/cost/energy metrics.
"""

import base64
import binascii
import json
import logging
from datetime import datetime, timedelta
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
from greenkube.core.config import get_config
from greenkube.storage.base_repository import CombinedMetricsRepository
from greenkube.utils.date_utils import time_range_from_last

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_time_range(last: Optional[str]) -> tuple[datetime, datetime]:
    """Compute (start, end) time range. Defaults to last 24h."""
    try:
        return time_range_from_last(last)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _decode_cursor(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(decoded, dict) or set(decoded) != {"timestamp", "namespace", "pod_name"}:
            raise ValueError
        return decoded
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as e:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from e


def _encode_cursor(value: Optional[dict]) -> Optional[str]:
    if value is None:
        return None
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


@router.get("/metrics", response_model=PaginatedMetricsResponse)
async def list_metrics(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', 'ytd')."),
    offset: int = Query(0, ge=0, description="Number of records to skip."),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum number of records to return."),
    cursor: Optional[str] = Query(None, description="Opaque cursor from a previous page."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """List combined metrics for the given time range and optional namespace filter.

    Uses DB-level COUNT + LIMIT/OFFSET pagination to avoid loading the full
    result set into memory.  The maximum allowed range is controlled by
    METRICS_LIST_MAX_RANGE_DAYS (default 30 days).
    """
    start, end = _get_time_range(last)
    cfg = get_config()
    max_days = cfg.METRICS_LIST_MAX_RANGE_DAYS
    if (end - start) > timedelta(days=max_days):
        raise HTTPException(
            status_code=400,
            detail=f"Requested range exceeds the {max_days}-day maximum for raw metric listing. "
            "Use the /report/export endpoint for bulk data export.",
        )
    decoded_cursor = _decode_cursor(cursor)
    if decoded_cursor is not None:
        cursor_reader = getattr(repo, "read_combined_metrics_cursor", None)
        if cursor_reader is not None:
            total, page, next_cursor = await cursor_reader(
                start_time=start, end_time=end, namespace=namespace, cursor=decoded_cursor, limit=min(limit, 1000)
            )
            return PaginatedMetricsResponse(
                total=total, offset=0, limit=min(limit, 1000), items=page, next_cursor=_encode_cursor(next_cursor)
            )
        # Compatibility fallback for repositories that predate cursor support.
        rows = await repo.read_combined_metrics_smart(start_time=start, end_time=end, namespace=namespace)
        total = len(rows)
        rows.sort(key=lambda item: (item.timestamp, item.namespace, item.pod_name))
        rows = [
            item
            for item in rows
            if item.timestamp is not None
            and (item.timestamp.isoformat(), item.namespace, item.pod_name)
            > (decoded_cursor["timestamp"], decoded_cursor["namespace"], decoded_cursor["pod_name"])
        ]
        page = rows[: min(limit, 1000)]
        next_value = None
        if len(rows) > len(page):
            last_metric = page[-1]
            assert last_metric.timestamp is not None
            next_value = {
                "timestamp": last_metric.timestamp.isoformat(),
                "namespace": last_metric.namespace,
                "pod_name": last_metric.pod_name,
            }
        return PaginatedMetricsResponse(
            total=total, offset=0, limit=min(limit, 1000), items=page, next_cursor=_encode_cursor(next_value)
        )
    total, page = await repo.read_combined_metrics_page(
        start_time=start, end_time=end, namespace=namespace, offset=offset, limit=limit
    )
    return PaginatedMetricsResponse(total=total, offset=offset, limit=limit, items=page)


@router.get("/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', 'ytd')."),
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
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', 'ytd')."),
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
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', 'ytd')."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return metrics aggregated by namespace (lightweight, SQL-level)."""
    start, end = _get_time_range(last)
    rows = await repo.aggregate_by_namespace(start_time=start, end_time=end, namespace=namespace)
    return [NamespaceBreakdownItem(**row) for row in rows]


@router.get("/metrics/top-pods", response_model=List[TopPodItem])
async def metrics_top_pods(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', 'ytd')."),
    limit: int = Query(10, ge=1, le=50, description="Number of top pods to return."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return top pods by CO2 emissions (lightweight, SQL-level)."""
    start, end = _get_time_range(last)
    rows = await repo.aggregate_top_pods(start_time=start, end_time=end, namespace=namespace, limit=limit)
    return [TopPodItem(**row) for row in rows]
