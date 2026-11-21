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
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from greenkube.api.dependencies import get_combined_metrics_repository, validate_namespace
from greenkube.api.routers.metrics import idle_history_gap, with_idle_share
from greenkube.api.schemas import ReportSummaryResponse
from greenkube.core.aggregator import aggregate_metrics
from greenkube.models.metrics import CombinedMetric
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


async def _aggregate_summary_for_ranges(
    repo: CombinedMetricsRepository,
    namespace: Optional[str],
    ranges: list[tuple[datetime, datetime]],
) -> dict:
    """Merge SQL-level aggregate_summary results across one or more time ranges.

    Scalars (CO2, cost, energy, row_count) are summed; pod/namespace uniqueness
    counts use max() across ranges — slightly conservative but avoids a full
    UNION query when multiple years are selected.
    """
    result: dict = {
        "row_count": 0,
        "total_co2e_grams": 0.0,
        "total_embodied_co2e_grams": 0.0,
        "total_cost": 0.0,
        "total_energy_joules": 0.0,
        "pod_count": 0,
        "namespace_count": 0,
    }
    for range_start, range_end in ranges:
        agg = await repo.aggregate_summary(start_time=range_start, end_time=range_end, namespace=namespace)
        result["row_count"] += agg.get("row_count", 0)
        result["total_co2e_grams"] += agg.get("total_co2e_grams", 0.0)
        result["total_embodied_co2e_grams"] += agg.get("total_embodied_co2e_grams", 0.0)
        result["total_cost"] += agg.get("total_cost", 0.0)
        result["total_energy_joules"] += agg.get("total_energy_joules", 0.0)
        result["pod_count"] = max(result["pod_count"], agg.get("pod_count", 0))
        result["namespace_count"] = max(result["namespace_count"], agg.get("namespace_count", 0))
    return result


async def _idle_share_for_ranges(
    repo: CombinedMetricsRepository,
    namespace: Optional[str],
    ranges: list[tuple[datetime, datetime]],
) -> dict:
    """Idle share and cluster totals of the report, summed over ``ranges`` (NRG-02).

    The node idle share belongs to no namespace: empty for a namespace report,
    and empty when no range holds an idle row (unknown, never zero) or when a
    range holds pod rows older than the first idle row (partial idle history).
    """
    if namespace:
        return {}
    idle: Optional[dict] = None
    since: Optional[datetime] = None
    for range_start, range_end in ranges:
        since, short = await idle_history_gap(repo, range_start, range_end)
        if short:
            return {"since": since}
        part = await repo.aggregate_idle_summary(range_start, range_end)
        if part is None:
            continue
        idle = idle or {}
        for key, value in part.items():
            idle[key] = idle.get(key, 0.0) + float(value or 0.0)
    return {"idle": idle, "since": since}


def _report_summary(total_rows: int, agg: dict, idle: dict) -> ReportSummaryResponse:
    """Report preview: allocated totals of ``agg``, plus the idle share and cluster totals when known."""
    total_co2e = agg["total_co2e_grams"]
    total_embodied = agg["total_embodied_co2e_grams"]
    return ReportSummaryResponse(
        total_rows=total_rows,
        total_co2e_grams=total_co2e,
        total_embodied_co2e_grams=total_embodied,
        total_co2e_all_scopes=total_co2e + total_embodied,
        total_cost=agg["total_cost"],
        total_energy_joules=agg["total_energy_joules"],
        unique_pods=agg["pod_count"],
        unique_namespaces=agg["namespace_count"],
        **({"idle_data_since": idle.get("since")} if idle else {}),
        **with_idle_share(agg, idle.get("idle")),
    )


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

    if not aggregate:
        # Fast path: SQL-level aggregation avoids loading millions of rows into memory.
        ranges = _get_time_ranges(last, start, end, years)
        agg = await _aggregate_summary_for_ranges(repo, namespace, ranges)
        return _report_summary(agg["row_count"], agg, await _idle_share_for_ranges(repo, namespace, ranges))

    # aggregate=True: use SQL-level grouped COUNT — no CombinedMetric rows are
    # loaded into memory regardless of the range width.
    # Totals (CO2, cost, energy, unique pods/namespaces) are identical to the
    # non-aggregate path and come from aggregate_summary.
    # Only total_rows differs: it's the number of (group_key × time_bucket)
    # combinations that the export would produce.
    resolved_ranges = _get_time_ranges(last, start, end, years)
    agg = await _aggregate_summary_for_ranges(repo, namespace, resolved_ranges)

    total_rows = 0
    for r_start, r_end in resolved_ranges:
        total_rows += await repo.aggregate_grouped_row_count(
            start_time=r_start,
            end_time=r_end,
            namespace=namespace,
            granularity=granularity,
            group_by=group_by,
        )

    return _report_summary(total_rows, agg, await _idle_share_for_ranges(repo, namespace, resolved_ranges))


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

    # Validate the requested range before starting the stream: once the
    # response has started, an invalid range cannot become a 400 anymore.
    ranges = _get_time_ranges(last, start, end, years)

    # The idle share and cluster totals of each range, computed before the stream starts so a
    # storage error is still an HTTP error (WP-05.3, NRG-02).
    idle_rows = [] if namespace else await _export_idle_rows(repo, ranges)

    # Stream results in chunks to avoid loading the whole dataset into memory.
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"greenkube-report-{timestamp_str}.{fmt}"

    # Create an async generator that yields bytes
    async def _iter_metrics_chunks(chunk_days: int = 7):
        # Convert requested ranges into a sequence of (start, end) windows
        for range_start, range_end in ranges:
            # iterate in chunks of chunk_days
            cursor = range_start
            delta = timedelta(days=chunk_days)
            while cursor <= range_end:
                window_end = min(range_end, cursor + delta)
                batch = await repo.read_combined_metrics_smart(
                    start_time=cursor, end_time=window_end, namespace=namespace
                )
                if aggregate:
                    granularity_flags = _granularity_flags(granularity)
                    batch = aggregate_metrics(batch, **granularity_flags, group_by=group_by)
                yield batch
                cursor = window_end + timedelta(seconds=1)

    if fmt == "json":

        async def _json_stream():
            first = True
            yield b"["
            async for batch in _iter_metrics_chunks():
                for m in batch:
                    if not first:
                        yield b","
                    else:
                        first = False
                    yield json.dumps(_allocated_row(m), ensure_ascii=False).encode("utf-8")
            for row in idle_rows:
                if not first:
                    yield b","
                first = False
                yield json.dumps(row, ensure_ascii=False).encode("utf-8")
            yield b"]"

        return StreamingResponse(
            _json_stream(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV streaming
    async def _csv_stream():
        header_written = False

        def _chunk(rows: list[dict]) -> bytes:
            nonlocal header_written
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=_EXPORT_COLUMNS)
            if not header_written:
                writer.writeheader()
                header_written = True
            for row in rows:
                writer.writerow({k: _sanitize_cell(row.get(k)) for k in _EXPORT_COLUMNS})
            return output.getvalue().encode("utf-8")

        async for batch in _iter_metrics_chunks():
            rows = [_allocated_row(m) for m in batch]
            if rows:
                yield _chunk(rows)
        if idle_rows:
            yield _chunk(idle_rows)

    return StreamingResponse(
        _csv_stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: ``row_kind`` of an exported workload row; the idle share and the cluster total follow them.
ROW_KIND_ALLOCATED = "allocated"
ROW_KIND_IDLE = "idle"
ROW_KIND_CLUSTER_TOTAL = "cluster_total"
#: Pseudo-workload names of the idle and cluster-total rows of an export.
IDLE_ROW_NAME = "__idle__"
CLUSTER_TOTAL_ROW_NAME = "__cluster_total__"

#: Export columns: every CombinedMetric field, then ``row_kind``.
_EXPORT_COLUMNS = [*CombinedMetric.model_fields, "row_kind"]

#: Export column → idle and cluster-total suffix of :func:`with_idle_share`.
_EXPORT_IDLE_COLUMNS = {
    "co2e_grams": "co2e_grams",
    "embodied_co2e_grams": "embodied_co2e_grams",
    "total_cost": "cost",
    "joules": "energy_joules",
}


def _allocated_row(metric: CombinedMetric) -> dict:
    """An exported workload row: the metric's fields, labelled ``row_kind = allocated``."""
    return {**metric.model_dump(mode="json"), "row_kind": ROW_KIND_ALLOCATED}


async def _export_idle_rows(repo: CombinedMetricsRepository, ranges: list[tuple[datetime, datetime]]) -> list[dict]:
    """One ``idle`` and one ``cluster_total`` row per report range (WP-05.3, NRG-02).

    The values follow the rule of ``/report/summary``: ``None`` (an empty CSV
    cell, a JSON ``null``) when the range holds no idle row or pod rows older
    than the first idle row (``idle_data_since``), never zero. ``period`` is
    the range as ``start/end`` (ISO 8601); the other metric columns are empty.
    A range without any pod or idle row gets no row.
    """
    rows: list[dict] = []
    for range_start, range_end in ranges:
        idle = await _idle_share_for_ranges(repo, None, [(range_start, range_end)])
        agg = await _aggregate_summary_for_ranges(repo, None, [(range_start, range_end)])
        if not agg["row_count"] and idle.get("idle") is None:
            continue  # nothing stored in the range: no row of unknown values
        share = with_idle_share(agg, idle.get("idle"))
        base = {
            "namespace": "",
            "period": f"{range_start.isoformat()}/{range_end.isoformat()}",
            "timestamp": range_start.isoformat(),
            "duration_seconds": int((range_end - range_start).total_seconds()),
        }
        for kind, name, prefix in (
            (ROW_KIND_IDLE, IDLE_ROW_NAME, "idle_"),
            (ROW_KIND_CLUSTER_TOTAL, CLUSTER_TOTAL_ROW_NAME, "cluster_total_"),
        ):
            row: dict = {column: None for column in _EXPORT_COLUMNS}
            row.update(base, pod_name=name, row_kind=kind, estimation_reasons=[])
            for column, suffix in _EXPORT_IDLE_COLUMNS.items():
                row[column] = share.get(f"{prefix}{suffix}")
            rows.append(row)
    return rows


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
    """Prevent CSV formula injection, including leading-whitespace bypasses."""
    s = str(value) if value is not None else ""
    if s.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{s}"
    return s
