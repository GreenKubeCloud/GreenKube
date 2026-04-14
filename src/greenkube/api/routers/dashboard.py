# src/greenkube/api/routers/dashboard.py
"""
API routes for the pre-computed dashboard summary table.

GET  /api/v1/metrics/dashboard-summary          — return cached rows
POST /api/v1/metrics/dashboard-summary/refresh  — trigger an on-demand refresh
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_summary_repository,
    validate_namespace,
)
from greenkube.api.schemas import DashboardSummaryResponse
from greenkube.storage.base_repository import CombinedMetricsRepository, SummaryRepository

logger = logging.getLogger(__name__)

router = APIRouter()


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
    except Exception as exc:
        logger.error("Failed to read dashboard summary: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read dashboard summary.") from exc

    return DashboardSummaryResponse(
        windows={row.window_slug: row for row in rows},
        namespace=namespace,
    )


@router.post("/metrics/dashboard-summary/refresh", status_code=202)
async def refresh_dashboard_summary(
    background_tasks: BackgroundTasks,
    namespace: Optional[str] = Depends(validate_namespace),
    metrics_repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    summary_repo: SummaryRepository = Depends(get_summary_repository),
):
    """Trigger an on-demand refresh of the dashboard summary rows.

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
        namespaces=namespaces,
    )

    async def _run():
        try:
            count = await refresher.run()
            logger.info("On-demand dashboard summary refresh complete: %d rows upserted.", count)
        except Exception as exc:
            logger.error("On-demand dashboard summary refresh failed: %s", exc)

    background_tasks.add_task(_run)
    return {"detail": "Dashboard summary refresh started."}
