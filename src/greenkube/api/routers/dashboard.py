# src/greenkube/api/routers/dashboard.py
"""
API routes for the pre-computed dashboard tables.

GET  /api/v1/metrics/dashboard-summary              — cached KPI scalars
GET  /api/v1/metrics/dashboard-timeseries/{slug}    — cached timeseries buckets
POST /api/v1/metrics/dashboard-summary/refresh      — trigger an on-demand refresh
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path

from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_summary_repository,
    get_timeseries_cache_repository,
    validate_namespace,
)
from greenkube.api.metrics_endpoint import update_dashboard_summary_metrics
from greenkube.api.schemas import DashboardSummaryResponse, DashboardTimeseriesResponse
from greenkube.storage.base_repository import (
    CombinedMetricsRepository,
    SummaryRepository,
    TimeseriesCacheRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Slugs that have pre-computed data
_VALID_SLUGS = {"1h", "6h", "24h", "7d", "30d", "1y", "ytd"}


@router.get("/metrics/dashboard-summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    summary_repo: SummaryRepository = Depends(get_summary_repository),
):
    """Return all pre-computed KPI summary rows.

    The data is refreshed hourly by the background scheduler.  If the table
    is empty (e.g. right after a first install), an empty response is returned
    — the frontend should fall back to the on-demand ``/metrics/summary``
    endpoint until the first refresh completes.
    """
    try:
        rows = await summary_repo.get_rows(namespace=namespace)
        update_dashboard_summary_metrics(rows, reset=namespace is None)
    except Exception as exc:
        logger.error("Failed to read dashboard summary: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read dashboard summary.") from exc

    return DashboardSummaryResponse(
        windows={row.window_slug: row for row in rows},
        namespace=namespace,
    )


@router.get(
    "/metrics/dashboard-timeseries/{window_slug}",
    response_model=DashboardTimeseriesResponse,
)
async def get_dashboard_timeseries(
    window_slug: str = Path(..., description="Time window slug: 1h, 6h, 24h, 7d, 30d, 1y, ytd."),
    namespace: Optional[str] = Depends(validate_namespace),
    ts_repo: TimeseriesCacheRepository = Depends(get_timeseries_cache_repository),
):
    """Return pre-computed time-series buckets for a specific window.

    The response maps directly to the shape expected by the frontend chart
    builders (``bucket_ts`` → x-axis, numeric fields → y-axis series).
    If no cached data exists yet, an empty list is returned so the
    frontend can fall back to the on-demand ``/metrics/timeseries`` endpoint.
    """
    if window_slug not in _VALID_SLUGS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid window slug '{window_slug}'. Valid values: {', '.join(sorted(_VALID_SLUGS))}.",
        )
    try:
        points = await ts_repo.get_points(window_slug=window_slug, namespace=namespace)
    except Exception as exc:
        logger.error("Failed to read timeseries cache for slug='%s': %s", window_slug, exc)
        raise HTTPException(status_code=500, detail="Failed to read timeseries cache.") from exc

    return DashboardTimeseriesResponse(
        window_slug=window_slug,
        namespace=namespace,
        points=points,
    )


@router.post("/metrics/dashboard-summary/refresh", status_code=202)
async def refresh_dashboard_summary(
    background_tasks: BackgroundTasks,
    namespace: Optional[str] = Depends(validate_namespace),
    metrics_repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    summary_repo: SummaryRepository = Depends(get_summary_repository),
    ts_repo: TimeseriesCacheRepository = Depends(get_timeseries_cache_repository),
):
    """Trigger an on-demand refresh of the dashboard summary and timeseries cache.

    The refresh runs in the background so the response is returned
    immediately (HTTP 202 Accepted).  The frontend can poll
    ``GET /metrics/dashboard-summary`` after a short delay to pick up
    the updated values.
    """
    from greenkube.core.summary_refresher import SummaryRefresher

    namespaces = [namespace] if namespace else None
    refresher = SummaryRefresher(
        metrics_repo=metrics_repo,
        summary_repo=summary_repo,
        timeseries_cache_repo=ts_repo,
        namespaces=namespaces,
    )

    async def _run():
        try:
            count = await refresher.run()
            rows = await summary_repo.get_rows(namespace=namespace)
            update_dashboard_summary_metrics(rows, reset=namespace is None)
            logger.info("On-demand dashboard refresh complete: %d rows upserted.", count)
        except Exception as exc:
            logger.error("On-demand dashboard refresh failed: %s", exc)

    background_tasks.add_task(_run)
    return {"detail": "Dashboard summary refresh started."}
