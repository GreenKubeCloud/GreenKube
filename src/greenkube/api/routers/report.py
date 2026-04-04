# src/greenkube/api/routers/report.py
"""
API routes for report generation and export.

Provides endpoints to generate FinGreenOps reports with filtering and
aggregation options, and to download them as CSV or JSON files.
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from greenkube.api.dependencies import get_combined_metrics_repository, validate_namespace
from greenkube.api.schemas import ReportSummaryResponse
from greenkube.core.aggregator import aggregate_metrics
from greenkube.storage.base_repository import CombinedMetricsRepository
from greenkube.utils.date_utils import parse_duration

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_FORMATS = ("csv", "json")
_VALID_GRANULARITIES = ("hourly", "daily", "weekly", "monthly", "yearly")


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


@router.get("/report/summary", response_model=ReportSummaryResponse)
async def report_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', '30d', '1y')."),
    aggregate: bool = Query(False, description="Aggregate rows by (namespace, pod, period)."),
    granularity: Optional[str] = Query(
        None,
        description="Time grouping when aggregate=true: 'hourly', 'daily', 'weekly', 'monthly', 'yearly'.",
    ),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return a preview summary of the report: row count and aggregated totals."""
    if granularity and granularity not in _VALID_GRANULARITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid granularity '{granularity}'. Valid values: {', '.join(_VALID_GRANULARITIES)}.",
        )

    start, end = _get_time_range(last)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)

    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]

    if aggregate:
        granularity_flags = _granularity_flags(granularity)
        metrics = aggregate_metrics(metrics, **granularity_flags)

    total_rows = len(metrics)
    total_co2e_grams = sum(m.co2e_grams for m in metrics)
    total_embodied_co2e_grams = sum(m.embodied_co2e_grams for m in metrics)
    total_cost = sum(m.total_cost for m in metrics)
    total_energy_joules = sum(m.joules for m in metrics)
    unique_pods = len({m.pod_name for m in metrics})
    unique_namespaces = len({m.namespace for m in metrics})

    return ReportSummaryResponse(
        total_rows=total_rows,
        total_co2e_grams=total_co2e_grams,
        total_embodied_co2e_grams=total_embodied_co2e_grams,
        total_cost=total_cost,
        total_energy_joules=total_energy_joules,
        unique_pods=unique_pods,
        unique_namespaces=unique_namespaces,
    )


@router.get("/report/export")
async def report_export(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', '30d', '1y')."),
    aggregate: bool = Query(False, description="Aggregate rows by (namespace, pod, period)."),
    granularity: Optional[str] = Query(
        None,
        description="Time grouping when aggregate=true: 'hourly', 'daily', 'weekly', 'monthly', 'yearly'.",
    ),
    fmt: str = Query("csv", alias="format", description="Export format: 'csv' or 'json'."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Generate and stream a report file in CSV or JSON format.

    The file is streamed directly to the browser for download.
    """
    if fmt not in _VALID_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format '{fmt}'. Valid values: {', '.join(_VALID_FORMATS)}.",
        )
    if granularity and granularity not in _VALID_GRANULARITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid granularity '{granularity}'. Valid values: {', '.join(_VALID_GRANULARITIES)}.",
        )

    start, end = _get_time_range(last)
    metrics = await repo.read_combined_metrics(start_time=start, end_time=end)

    if namespace:
        metrics = [m for m in metrics if m.namespace == namespace]

    if aggregate:
        granularity_flags = _granularity_flags(granularity)
        metrics = aggregate_metrics(metrics, **granularity_flags)

    rows = [m.model_dump(mode="json") for m in metrics]

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"greenkube-report-{timestamp_str}.{fmt}"

    if fmt == "json":
        content = json.dumps(rows, ensure_ascii=False, indent=2)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV export
    content = _rows_to_csv(rows)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _granularity_flags(granularity: Optional[str]) -> dict:
    """Convert a granularity string into keyword arguments for aggregate_metrics."""
    flags = {g: False for g in _VALID_GRANULARITIES}
    if granularity and granularity in flags:
        flags[granularity] = True
    return flags


def _rows_to_csv(rows: list) -> str:
    """Serialize a list of dicts to a CSV string."""
    if not rows:
        return ""
    output = io.StringIO()
    headers = list(rows[0].keys())
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        sanitized = {k: _sanitize_cell(v) for k, v in row.items()}
        writer.writerow(sanitized)
    return output.getvalue()


def _sanitize_cell(value) -> str:
    """Prevent CSV formula injection."""
    s = str(value) if value is not None else ""
    if s.startswith(("=", "+", "-", "@")):
        return f"'{s}"
    return s
