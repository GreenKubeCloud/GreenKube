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
from datetime import datetime, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from greenkube.api.dependencies import get_combined_metrics_repository, validate_namespace
from greenkube.api.schemas import ReportSummaryResponse
from greenkube.core.aggregator import aggregate_metrics
from greenkube.storage.base_repository import CombinedMetricsRepository
from greenkube.utils.date_utils import ensure_utc, time_range_from_last

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_FORMATS = ("csv", "json")
_VALID_GRANULARITIES = ("hourly", "daily", "weekly", "monthly", "yearly")
_VALID_GROUP_BY = ("pod", "namespace")


def _get_time_range(last: Optional[str]) -> tuple[datetime, datetime]:
    """Compute (start, end) time range. Defaults to last 24h."""
    try:
        return time_range_from_last(last)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _get_calendar_year_range(year: int) -> tuple[datetime, datetime]:
    """Return UTC day boundaries for a full calendar year."""
    if year < 1 or year > 9999:
        raise HTTPException(status_code=400, detail=f"Invalid year '{year}'.")
    return (
        datetime(year, 1, 1, tzinfo=timezone.utc),
        datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
    )


def _is_date_only(value: str) -> bool:
    """Return True when value is a YYYY-MM-DD date without a time component."""
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def _parse_report_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse a report date/datetime query value into a UTC datetime."""
    try:
        clean_value = value.strip()
        if _is_date_only(clean_value):
            parsed_date = datetime.fromisoformat(clean_value).date()
            boundary = time.max if end_of_day else time.min
            return datetime.combine(parsed_date, boundary, tzinfo=timezone.utc)
        return ensure_utc(clean_value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date '{value}'.") from e


def _get_time_ranges(
    last: Optional[str],
    start: Optional[str],
    end: Optional[str],
    years: Optional[list[int]],
) -> list[tuple[datetime, datetime]]:
    """Resolve relative, custom, or yearly report filters into UTC ranges."""
    selected_years = list(dict.fromkeys(years or []))
    if selected_years:
        if last or start or end:
            raise HTTPException(status_code=400, detail="Use either years, custom dates, or last; not more than one.")
        return [_get_calendar_year_range(year) for year in selected_years]

    if start or end:
        if last:
            raise HTTPException(status_code=400, detail="Use either custom dates or last; not both.")
        if not start or not end:
            raise HTTPException(status_code=400, detail="Both start and end are required for a custom report range.")
        start_dt = _parse_report_datetime(start)
        end_dt = _parse_report_datetime(end, end_of_day=_is_date_only(end.strip()))
        if start_dt > end_dt:
            raise HTTPException(status_code=400, detail="Report start date must be before end date.")
        return [(start_dt, end_dt)]

    return [_get_time_range(last)]


async def _read_report_metrics(
    repo: CombinedMetricsRepository,
    namespace: Optional[str],
    last: Optional[str],
    start: Optional[str],
    end: Optional[str],
    years: Optional[list[int]],
) -> list:
    """Read report metrics for one or more resolved time ranges."""
    metrics = []
    for range_start, range_end in _get_time_ranges(last, start, end, years):
        metrics.extend(
            await repo.read_combined_metrics_smart(start_time=range_start, end_time=range_end, namespace=namespace)
        )
    return metrics


@router.get("/report/summary", response_model=ReportSummaryResponse)
async def report_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', '30d', '1y', 'ytd')."),
    start: Optional[str] = Query(None, description="Custom report start date or datetime."),
    end: Optional[str] = Query(None, description="Custom report end date or datetime."),
    years: Optional[list[int]] = Query(None, description="Calendar years to include, repeatable."),
    aggregate: bool = Query(False, description="Aggregate rows by (namespace, pod, period)."),
    granularity: Optional[str] = Query(
        None,
        description="Time grouping when aggregate=true: 'hourly', 'daily', 'weekly', 'monthly', 'yearly'.",
    ),
    group_by: str = Query("pod", description="Aggregation grouping: 'pod' or 'namespace'."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return a preview summary of the report: row count and aggregated totals."""
    if granularity and granularity not in _VALID_GRANULARITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid granularity '{granularity}'. Valid values: {', '.join(_VALID_GRANULARITIES)}.",
        )
    if group_by not in _VALID_GROUP_BY:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid group_by '{group_by}'. Valid values: {', '.join(_VALID_GROUP_BY)}.",
        )

    raw_metrics = await _read_report_metrics(repo, namespace, last, start, end, years)
    metrics = raw_metrics

    if aggregate:
        granularity_flags = _granularity_flags(granularity)
        metrics = aggregate_metrics(metrics, **granularity_flags, group_by=group_by)

    total_rows = len(metrics)
    total_co2e_grams = sum(m.co2e_grams for m in metrics)
    total_embodied_co2e_grams = sum(m.embodied_co2e_grams for m in metrics)
    total_cost = sum(m.total_cost for m in metrics)
    total_energy_joules = sum(m.joules for m in metrics)
    unique_pods = len({m.pod_name for m in raw_metrics})
    unique_namespaces = len({m.namespace for m in raw_metrics})

    return ReportSummaryResponse(
        total_rows=total_rows,
        total_co2e_grams=total_co2e_grams,
        total_embodied_co2e_grams=total_embodied_co2e_grams,
        total_co2e_all_scopes=total_co2e_grams + total_embodied_co2e_grams,
        total_cost=total_cost,
        total_energy_joules=total_energy_joules,
        unique_pods=unique_pods,
        unique_namespaces=unique_namespaces,
    )


@router.get("/report/years", response_model=list[int])
async def report_years(
    namespace: Optional[str] = Depends(validate_namespace),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
):
    """Return calendar years that have reportable metric data."""
    return await repo.list_metric_years(namespace=namespace)


@router.get("/report/export")
async def report_export(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '10min', '2h', '7d', '30d', '1y', 'ytd')."),
    start: Optional[str] = Query(None, description="Custom report start date or datetime."),
    end: Optional[str] = Query(None, description="Custom report end date or datetime."),
    years: Optional[list[int]] = Query(None, description="Calendar years to include, repeatable."),
    aggregate: bool = Query(False, description="Aggregate rows by (namespace, pod, period)."),
    granularity: Optional[str] = Query(
        None,
        description="Time grouping when aggregate=true: 'hourly', 'daily', 'weekly', 'monthly', 'yearly'.",
    ),
    group_by: str = Query("pod", description="Aggregation grouping: 'pod' or 'namespace'."),
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
    if group_by not in _VALID_GROUP_BY:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid group_by '{group_by}'. Valid values: {', '.join(_VALID_GROUP_BY)}.",
        )

    metrics = await _read_report_metrics(repo, namespace, last, start, end, years)

    if aggregate:
        granularity_flags = _granularity_flags(granularity)
        metrics = aggregate_metrics(metrics, **granularity_flags, group_by=group_by)

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
