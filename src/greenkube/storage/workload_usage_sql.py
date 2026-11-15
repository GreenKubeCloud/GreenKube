"""SQL shared by the SQLite and PostgreSQL workload usage repositories (roadmap E3).

The queries are written once with ``{p1}``-style placeholders that each backend
formats (``?`` or ``$n``). Every scan read is bounded by the number of targets,
containers and nodes, never by the number of samples in the lookback.

Lookback percentiles are nearest-rank percentiles of the hourly percentiles,
computed with window functions: within each target container, hours are ranked
by their hourly value and the hour at rank ``ceil(p * hours / 100)`` (integer
form ``(p * hours + 99) / 100``) gives the lookback value. This is the same rule
as :func:`greenkube.core.workload_usage.nearest_rank`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from greenkube.models.provenance import cost_source_aggregate_sql
from greenkube.models.workload_usage import (
    POD_LEVEL,
    USAGE_PERCENTILES,
    BaselineMetric,
    NodeUsageSummary,
    TargetKey,
    UsageSnapshot,
    UsageStats,
    WorkloadUsageHour,
    WorkloadUsageSummary,
)
from greenkube.storage.base_workload_usage_repository import INPUT_TABLES, ROLLUP_COLUMNS

_KEY = "namespace, target_kind, target_name, container_name"

#: Pod-level metric columns the rollup reads, as ``(select expression, UsageInputRow field)``.
RAW_POD_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("namespace", "namespace"),
    ("pod_name", "pod_name"),
    ("{ts}", "timestamp"),
    ("owner_kind", "owner_kind"),
    ("owner_name", "owner_name"),
    ("node", "node"),
    ("node_instance_type", "node_instance_type"),
    ("duration_seconds", "duration_seconds"),
    ("cpu_usage_millicores", "cpu_usage_millicores"),
    ("memory_usage_bytes", "memory_usage_bytes"),
    ("cpu_request", "cpu_request"),
    ("memory_request", "memory_request"),
    ("restart_count", "restart_count"),
    ("total_cost", "total_cost"),
    ("cost_cpu", "cost_cpu"),
    ("cost_ram", "cost_ram"),
    ("cost_storage", "cost_storage"),
    ("cost_network", "cost_network"),
    ("cost_lb", "cost_lb"),
    ("cost_source", "cost_source"),
    ("co2e_grams", "co2e_grams"),
    ("embodied_co2e_grams", "embodied_co2e_grams"),
    ("joules", "joules"),
    ("gpu_power_watts", "gpu_power_watts"),
    ("pue", "pue"),
    ("grid_intensity", "grid_intensity"),
    ("emaps_zone", "emaps_zone"),
)

RAW_CONTAINER_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("namespace", "namespace"),
    ("pod_name", "pod_name"),
    ("container_name", "container_name"),
    ("{ts}", "timestamp"),
    ("owner_kind", "owner_kind"),
    ("owner_name", "owner_name"),
    ("duration_seconds", "duration_seconds"),
    ("cpu_usage_millicores", "cpu_usage_millicores"),
    ("cpu_usage_max_millicores", "cpu_usage_max_millicores"),
    ("memory_usage_bytes", "memory_usage_bytes"),
    ("memory_usage_max_bytes", "memory_usage_max_bytes"),
    ("cpu_request", "cpu_request"),
    ("memory_request", "memory_request"),
    ("cpu_limit", "cpu_limit"),
    ("memory_limit", "memory_limit"),
    ("total_cost", "total_cost"),
    ("cost_cpu", "cost_cpu"),
    ("cost_ram", "cost_ram"),
    ("cost_storage", "cost_storage"),
    ("cost_network", "cost_network"),
    ("cost_lb", "cost_lb"),
    ("cost_source", "cost_source"),
    ("co2e_grams", "co2e_grams"),
    ("embodied_co2e_grams", "embodied_co2e_grams"),
    ("joules", "joules"),
    ("gpu_power_watts", "gpu_power_watts"),
    ("pue", "pue"),
    ("grid_intensity", "grid_intensity"),
    ("emaps_zone", "emaps_zone"),
)


def _hourly_columns(raw: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[str, str], ...]:
    """The same fields read from a compressed ``*_hourly`` table."""
    renamed = {
        "{ts}": "hour_bucket",
        "cpu_usage_millicores": "cpu_usage_avg",
        "memory_usage_bytes": "memory_usage_avg",
        "cpu_usage_max_millicores": "cpu_usage_max",
        "memory_usage_max_bytes": "memory_usage_max",
        # ``gpu_power_watts`` is the hour's duration-weighted average (migration 0064).
    }
    columns = [(renamed.get(expr, expr), name) for expr, name in raw]
    names = {name for _, name in columns}
    if "cpu_usage_max_millicores" not in names:
        columns.append(("cpu_usage_max", "cpu_usage_max_millicores"))
        columns.append(("memory_usage_max", "memory_usage_max_bytes"))
    columns.append(("sample_count", "sample_count"))
    return tuple(columns)


HOURLY_POD_COLUMNS = _hourly_columns(RAW_POD_COLUMNS)
HOURLY_CONTAINER_COLUMNS = _hourly_columns(RAW_CONTAINER_COLUMNS)


def select_list(columns: Tuple[Tuple[str, str], ...], ts: str) -> str:
    return ", ".join(expr.format(ts=ts) for expr, _ in columns)


# --------------------------------------------------------------------------- #
# Rollup writes                                                                #
# --------------------------------------------------------------------------- #


def upsert_sql(placeholders: Sequence[str]) -> str:
    names = ", ".join(ROLLUP_COLUMNS)
    identity = ("hour_bucket", "namespace", "target_kind", "target_name", "container_name", "node")
    updates = ", ".join(f"{column} = excluded.{column}" for column in ROLLUP_COLUMNS if column not in identity)
    return (
        f"INSERT INTO workload_usage_hourly ({names}) VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({', '.join(identity)}) DO UPDATE SET {updates}"
    )


def mark_ranges(after: Mapping[str, int], upto: Mapping[str, int]) -> List[Tuple[str, int, int]]:
    """Return ``(table, lower, upper)`` id ranges of the raw rows written since ``after``.

    A table without a saved mark, or whose ids went backwards (table emptied
    and recreated), is read from id 0 so that no row is missed.
    """
    ranges: List[Tuple[str, int, int]] = []
    for table in INPUT_TABLES:
        upper = int(upto.get(table, 0) or 0)
        lower = int(after.get(table, 0) or 0)
        if lower > upper:
            lower = 0
        if upper > lower:
            ranges.append((table, lower, upper))
    return ranges


# --------------------------------------------------------------------------- #
# Scan reads                                                                   #
# --------------------------------------------------------------------------- #


def scope_params(
    start: Any, end: Any, namespace: Optional[str], namespaces: Optional[Sequence[str]] = None
) -> Tuple[Any, ...]:
    """Parameters matching :func:`_window_filter` placeholders, in order."""
    values: Tuple[Any, ...] = (start, end)
    if namespace is not None:
        values = (*values, namespace)
    if namespaces is not None:
        values = (*values, *namespaces)
    return values


def _window_filter(
    ph: Callable[[int], str],
    namespace: Optional[str],
    extra: str = "",
    namespaces: Optional[Sequence[str]] = None,
) -> str:
    where = f"hour_bucket >= {ph(1)} AND hour_bucket < {ph(2)}"
    index = 3
    if namespace is not None:
        where += f" AND namespace = {ph(index)}"
        index += 1
    if namespaces is not None:
        # Namespaces that still exist: history of deleted ones is left out of
        # summaries, node averages and the observed window alike.
        if namespaces:
            where += f" AND namespace IN ({', '.join(ph(index + offset) for offset in range(len(namespaces)))})"
        else:
            where += " AND 1 = 0"
    if extra:
        where += f" AND {extra}"
    return where


def summary_sql(ph: Callable[[int], str], namespace: Optional[str], namespaces: Optional[Sequence[str]] = None) -> str:
    """One row per target container over the window (see the module docstring)."""
    partition = f"PARTITION BY {_KEY}"
    ranks = []
    picks = []
    for resource, alias in (("cpu", "cpu"), ("memory", "mem")):
        for p in USAGE_PERCENTILES:
            ranks.append(
                f"ROW_NUMBER() OVER ({partition} ORDER BY CASE WHEN {alias}_n > 0 THEN 0 ELSE 1 END, "
                f"{alias}_p{p}) AS r_{alias}_p{p}"
            )
            picks.append(
                f"MAX(CASE WHEN {alias}_n > 0 AND r_{alias}_p{p} = ({p} * {alias}_hours + 99) / 100 "
                f"THEN {alias}_p{p} END) AS {alias}_q{p}"
            )
        picks.append(f"MAX(CASE WHEN {alias}_n > 0 THEN {alias}_max END) AS {alias}_qmax")
    percentile_columns = []
    for resource, alias in (("cpu", "cpu"), ("memory", "mem")):
        for p in USAGE_PERCENTILES:
            percentile_columns.append(f"MAX({resource}_usage_p{p}) AS {alias}_p{p}")
        percentile_columns.append(f"MAX({resource}_usage_max) AS {alias}_max")
    # REC-02: the latest hour's requests and limits, and the last hour that had other values (the
    # same rule as ``_config_since`` in core/workload_usage.py: requests default to 0, limits to NULL).
    defaults = {"cpu_request": "0", "memory_request": "0", "cpu_limit": "-1", "memory_limit": "-1"}
    current_config = [
        f"FIRST_VALUE({column}) OVER ({partition} ORDER BY hour_bucket DESC) AS current_{column}" for column in defaults
    ]

    def _differs(columns: Sequence[str]) -> str:
        return " OR ".join(
            f"COALESCE({column}, {defaults[column]}) <> COALESCE(current_{column}, {defaults[column]})"
            for column in columns
        )

    known_request = "(COALESCE(memory_request, 0) > 0 OR COALESCE(cpu_request, 0) > 0)"
    memory_differs = _differs(("memory_request", "memory_limit"))
    resources_differ = _differs(tuple(defaults))
    return f"""
        WITH scoped AS (
            SELECT workload_usage_hourly.*,
                COALESCE(latest_sample_offset_seconds, -1) AS row_offset,
                MAX(COALESCE(latest_sample_offset_seconds, -1)) OVER ({partition}, hour_bucket) AS hour_offset
            FROM workload_usage_hourly
            WHERE {_window_filter(ph, namespace, namespaces=namespaces)}
        ),
        hourly AS (
            SELECT {_KEY}, hour_bucket,
                SUM(sample_count) AS sample_count,
                MAX(step_count) AS step_count,
                SUM(pod_count) AS pod_count,
                SUM(duration_seconds) AS duration_seconds,
                SUM(cpu_sample_count) AS cpu_n,
                SUM(COALESCE(cpu_usage_avg, 0) * cpu_sample_count) AS cpu_sum,
                SUM(COALESCE(cpu_usage_sq_avg, 0) * cpu_sample_count) AS cpu_sq_sum,
                SUM(memory_sample_count) AS mem_n,
                SUM(COALESCE(memory_usage_avg, 0) * memory_sample_count) AS mem_sum,
                {", ".join(percentile_columns)},
                MAX(CASE WHEN row_offset = hour_offset THEN cpu_request END) AS cpu_request,
                MAX(CASE WHEN row_offset = hour_offset THEN memory_request END) AS memory_request,
                MAX(CASE WHEN row_offset = hour_offset THEN cpu_limit END) AS cpu_limit,
                MAX(CASE WHEN row_offset = hour_offset THEN memory_limit END) AS memory_limit,
                MAX(restart_count) AS restart_count,
                SUM(restart_increase) AS restart_increase,
                MAX(CASE WHEN restart_count IS NOT NULL AND restart_increase IS NULL THEN 1 ELSE 0 END)
                    AS restart_increase_unknown,
                SUM(total_cost) AS total_cost,
                SUM(co2e_grams) AS co2e_grams,
                SUM(embodied_co2e_grams) AS embodied_co2e_grams,
                SUM(joules) AS joules,
                SUM(COALESCE(grid_intensity, 0) * intensity_sample_count) AS intensity_sum,
                SUM(intensity_sample_count) AS intensity_n,
                MAX(emaps_zone) AS emaps_zone
            FROM scoped
            GROUP BY {_KEY}, hour_bucket
        ),
        ranked AS (
            SELECT hourly.*,
                MAX(hour_bucket) OVER ({partition}) AS last_hour,
                SUM(CASE WHEN cpu_n > 0 THEN 1 ELSE 0 END) OVER ({partition}) AS cpu_hours,
                SUM(CASE WHEN mem_n > 0 THEN 1 ELSE 0 END) OVER ({partition}) AS mem_hours,
                {", ".join(current_config)},
                {", ".join(ranks)}
            FROM hourly
        )
        SELECT {_KEY},
            MIN(hour_bucket) AS first_hour,
            MAX(hour_bucket) AS latest_hour,
            COUNT(*) AS hours_observed,
            SUM(sample_count) AS sample_count,
            SUM(step_count) AS step_count,
            SUM(duration_seconds) AS duration_seconds,
            MAX(pod_count) AS replicas_max,
            SUM(cpu_n) AS cpu_count, SUM(cpu_sum) AS cpu_total, SUM(cpu_sq_sum) AS cpu_sq_total,
            SUM(mem_n) AS mem_count, SUM(mem_sum) AS mem_total,
            {", ".join(picks)},
            MAX(CASE WHEN hour_bucket = last_hour THEN cpu_request END) AS latest_cpu_request,
            MAX(CASE WHEN hour_bucket = last_hour THEN memory_request END) AS latest_memory_request,
            MAX(CASE WHEN hour_bucket = last_hour THEN cpu_limit END) AS latest_cpu_limit,
            MAX(CASE WHEN hour_bucket = last_hour THEN memory_limit END) AS latest_memory_limit,
            MAX(restart_count) AS restart_count,
            CASE WHEN COUNT(restart_count) = 0 AND COUNT(restart_increase) = 0 THEN NULL
                WHEN MAX(restart_increase_unknown) = 1 THEN NULL
                ELSE SUM(COALESCE(restart_increase, 0)) END AS restart_increase,
            MAX(restart_increase_unknown) AS restart_increase_legacy,
            MAX(CASE WHEN {known_request} AND ({memory_differs}) THEN hour_bucket END) AS memory_changed_hour,
            MAX(CASE WHEN {known_request} AND ({resources_differ}) THEN hour_bucket END) AS resources_changed_hour,
            SUM(total_cost) AS total_cost,
            SUM(co2e_grams) AS co2e_grams,
            SUM(embodied_co2e_grams) AS embodied_co2e_grams,
            SUM(joules) AS joules,
            SUM(intensity_sum) AS intensity_sum,
            SUM(intensity_n) AS intensity_n,
            MAX(emaps_zone) AS emaps_zone
        FROM ranked
        GROUP BY {_KEY}
    """


def hour_profile_sql(
    ph: Callable[[int], str], namespace: Optional[str], namespaces: Optional[Sequence[str]] = None
) -> str:
    return f"""
        SELECT namespace, target_kind, target_name, hour_of_day,
            SUM(cpu_usage_avg * cpu_sample_count) AS cpu_sum,
            SUM(cpu_sample_count) AS cpu_n
        FROM workload_usage_hourly
        WHERE {_window_filter(ph, namespace, f"container_name = '{POD_LEVEL}' AND cpu_sample_count > 0", namespaces)}
        GROUP BY namespace, target_kind, target_name, hour_of_day
    """


def node_sql(ph: Callable[[int], str], namespace: Optional[str], namespaces: Optional[Sequence[str]] = None) -> str:
    return f"""
        WITH per_node AS (
            SELECT node, hour_bucket,
                MAX(step_count) AS steps,
                SUM(COALESCE(cpu_usage_avg, 0) * cpu_sample_count) AS cpu_sum,
                SUM(COALESCE(memory_usage_avg, 0) * memory_sample_count) AS mem_sum,
                SUM(cpu_request_sum) AS cpu_req,
                SUM(memory_request_sum) AS mem_req
            FROM workload_usage_hourly
            WHERE {_window_filter(ph, namespace, f"container_name = '{POD_LEVEL}' AND node <> ''", namespaces)}
            GROUP BY node, hour_bucket
        )
        SELECT node, COUNT(*) AS hours_observed, SUM(steps) AS steps,
            SUM(cpu_sum) AS cpu_sum, SUM(mem_sum) AS mem_sum, SUM(cpu_req) AS cpu_req, SUM(mem_req) AS mem_req
        FROM per_node
        GROUP BY node
    """


_HOD = f"CASE WHEN container_name = '{POD_LEVEL}' THEN hour_of_day ELSE -1 END"


def baseline_sql(ph: Callable[[int], str], namespace: Optional[str], namespaces: Optional[Sequence[str]] = None) -> str:
    """Rows aggregated for projections: pod level by hour of day, containers without it."""
    return f"""
        SELECT {_KEY}, node, {_HOD} AS hod, cpu_request, memory_request,
            MAX(hour_bucket) AS last_hour,
            SUM(sample_count) AS sample_count,
            SUM(duration_seconds) AS duration_seconds,
            SUM(cpu_sample_count) AS cpu_n,
            SUM(COALESCE(cpu_usage_avg, 0) * cpu_sample_count) AS cpu_sum,
            MAX(cpu_usage_max) AS cpu_max,
            SUM(memory_sample_count) AS mem_n,
            SUM(COALESCE(memory_usage_avg, 0) * memory_sample_count) AS mem_sum,
            MAX(memory_usage_max) AS mem_max,
            MAX(cpu_limit) AS cpu_limit,
            MAX(memory_limit) AS memory_limit,
            MAX(restart_count) AS restart_count,
            SUM(total_cost) AS total_cost,
            SUM(cost_cpu) AS cost_cpu,
            SUM(cost_ram) AS cost_ram,
            SUM(cost_storage) AS cost_storage,
            SUM(cost_network) AS cost_network,
            SUM(cost_lb) AS cost_lb,
            {cost_source_aggregate_sql("cost_source")} AS cost_source,
            SUM(co2e_grams) AS co2e_grams,
            SUM(embodied_co2e_grams) AS embodied_co2e_grams,
            SUM(joules) AS joules,
            SUM(gpu_watt_seconds) AS gpu_watt_seconds,
            SUM(CASE WHEN gpu_watt_seconds IS NOT NULL THEN duration_seconds ELSE 0 END) AS gpu_seconds,
            SUM(COALESCE(pue, 0) * sample_count) AS pue_sum,
            SUM(CASE WHEN pue IS NOT NULL THEN sample_count ELSE 0 END) AS pue_n,
            SUM(COALESCE(grid_intensity, 0) * intensity_sample_count) AS intensity_sum,
            SUM(intensity_sample_count) AS intensity_n,
            MAX(emaps_zone) AS emaps_zone,
            MAX(node_instance_type) AS node_instance_type
        FROM workload_usage_hourly
        WHERE {_window_filter(ph, namespace, namespaces=namespaces)}
        GROUP BY {_KEY}, node, {_HOD}, cpu_request, memory_request
    """


# --------------------------------------------------------------------------- #
# Row mapping                                                                  #
# --------------------------------------------------------------------------- #


def _f(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _i(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _stats(row: Mapping[str, Any], alias: str, sq: bool) -> Optional[UsageStats]:
    n = int(row[f"{alias}_count"] or 0)
    if n <= 0:
        return None
    avg = float(row[f"{alias}_total"] or 0.0) / n
    stddev = None
    if sq:
        variance = max(float(row[f"{alias}_sq_total"] or 0.0) / n - avg * avg, 0.0)
        stddev = variance**0.5

    def _value(name: str) -> float:
        value = row[name]
        return float(value) if value is not None else avg

    return UsageStats(
        sample_count=n,
        avg=avg,
        p50=_value(f"{alias}_q50"),
        p90=_value(f"{alias}_q90"),
        p95=_value(f"{alias}_q95"),
        p99=_value(f"{alias}_q99"),
        max=_value(f"{alias}_qmax"),
        stddev=stddev,
    )


def _hour_end(bucket: Optional[datetime]) -> Optional[datetime]:
    """End of an hour bucket, or None (REC-02: since when the configuration is current)."""
    return bucket + timedelta(hours=1) if bucket is not None else None


def summary_from_row(row: Mapping[str, Any], decode_time: Callable[[Any], Optional[datetime]]) -> WorkloadUsageSummary:
    intensity_n = int(row["intensity_n"] or 0)
    return WorkloadUsageSummary(
        namespace=row["namespace"],
        target_kind=row["target_kind"],
        target_name=row["target_name"],
        container_name=row["container_name"] or POD_LEVEL,
        first_hour=decode_time(row["first_hour"]),
        last_hour=decode_time(row["latest_hour"]),
        hours_observed=int(row["hours_observed"] or 0),
        sample_count=int(row["sample_count"] or 0),
        step_count=int(row["step_count"] or 0),
        duration_seconds=float(row["duration_seconds"] or 0.0),
        replicas_max=int(row["replicas_max"] or 0),
        cpu=_stats(row, "cpu", sq=True),
        memory=_stats(row, "mem", sq=False),
        cpu_request=int(row["latest_cpu_request"] or 0),
        memory_request=int(row["latest_memory_request"] or 0),
        cpu_limit=_i(row["latest_cpu_limit"]),
        memory_limit=_i(row["latest_memory_limit"]),
        restart_count=_i(row["restart_count"]),
        restart_increase=_i(row["restart_increase"]),
        restart_increase_legacy=bool(row["restart_increase_legacy"] or 0),
        memory_config_since=_hour_end(decode_time(row["memory_changed_hour"])),
        resources_config_since=_hour_end(decode_time(row["resources_changed_hour"])),
        total_cost=float(row["total_cost"] or 0.0),
        co2e_grams=float(row["co2e_grams"] or 0.0),
        embodied_co2e_grams=float(row["embodied_co2e_grams"] or 0.0),
        joules=float(row["joules"] or 0.0),
        grid_intensity=(float(row["intensity_sum"] or 0.0) / intensity_n) if intensity_n else None,
        intensity_sample_count=intensity_n,
        emaps_zone=row["emaps_zone"],
    )


def build_snapshot(
    summary_rows: Sequence[Mapping[str, Any]],
    profile_rows: Sequence[Mapping[str, Any]],
    node_rows: Sequence[Mapping[str, Any]],
    decode_time: Callable[[Any], Optional[datetime]],
) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="rollup")
    for row in summary_rows:
        summary = summary_from_row(row, decode_time)
        snapshot.summaries.setdefault(summary.target, {})[summary.container_name] = summary
        if summary.first_hour is not None and (snapshot.first_hour is None or summary.first_hour < snapshot.first_hour):
            snapshot.first_hour = summary.first_hour

    sums: Dict[TargetKey, List[float]] = {}
    counts: Dict[TargetKey, List[int]] = {}
    for row in profile_rows:
        target = (row["namespace"], row["target_kind"], row["target_name"])
        hour = int(row["hour_of_day"])
        sums.setdefault(target, [0.0] * 24)[hour] += float(row["cpu_sum"] or 0.0)
        counts.setdefault(target, [0] * 24)[hour] += int(row["cpu_n"] or 0)
    for target, values in sums.items():
        n = counts[target]
        snapshot.hour_profiles[target] = tuple(values[h] / n[h] if n[h] else None for h in range(24))
        snapshot.hour_profile_samples[target] = sum(n)

    for row in node_rows:
        steps = float(row["steps"] or 0.0)
        snapshot.nodes[row["node"]] = NodeUsageSummary(
            node=row["node"],
            hours_observed=int(row["hours_observed"] or 0),
            cpu_usage_avg=float(row["cpu_sum"] or 0.0) / steps if steps else 0.0,
            memory_usage_avg=float(row["mem_sum"] or 0.0) / steps if steps else 0.0,
            cpu_request_avg=float(row["cpu_req"] or 0.0) / steps if steps else 0.0,
            memory_request_avg=float(row["mem_req"] or 0.0) / steps if steps else 0.0,
        )
    return snapshot


def _rounded(total: Any, count: Any) -> Optional[int]:
    n = int(count or 0)
    if n <= 0:
        return None
    return int(round(float(total or 0.0) / n))


#: One shared "fields set" for every baseline row: a per-row set would double
#: the memory of a row (scans hold thousands of them).
_BASELINE_FIELDS = set(BaselineMetric.model_fields)


def baseline_from_row(
    row: Mapping[str, Any],
    decode_time: Callable[[Any], Optional[datetime]],
    summaries: Mapping[TargetKey, Mapping[str, WorkloadUsageSummary]],
) -> BaselineMetric:
    container = row["container_name"] or POD_LEVEL
    kind = row["target_kind"]
    name = row["target_name"]
    target = (row["namespace"], kind, name)
    summary = summaries.get(target, {}).get(container)
    cost_source = row["cost_source"]
    pue_n = int(row["pue_n"] or 0)
    intensity_n = int(row["intensity_n"] or 0)
    cpu_max = _f(row["cpu_max"])
    memory_max = _f(row["mem_max"])
    gpu_seconds = float(row["gpu_seconds"] or 0.0)
    gpu_watts = float(row["gpu_watt_seconds"] or 0.0) / gpu_seconds if gpu_seconds > 0 else None
    values: Dict[str, Any] = {
        "pod_name": name,
        "namespace": row["namespace"],
        "container_name": container or None,
        "owner_kind": None if kind == "Pod" else kind,
        "owner_name": None if kind == "Pod" else name,
        "node": row["node"] or None,
        "node_instance_type": row["node_instance_type"],
        "timestamp": decode_time(row["last_hour"]),
        "sample_count": int(row["sample_count"] or 1),
        "duration_seconds": int(round(float(row["duration_seconds"] or 0.0))) or None,
        "cpu_usage_millicores": _rounded(row["cpu_sum"], row["cpu_n"]),
        "cpu_usage_max_millicores": int(round(cpu_max)) if cpu_max is not None else None,
        "memory_usage_bytes": _rounded(row["mem_sum"], row["mem_n"]),
        "memory_usage_max_bytes": int(round(memory_max)) if memory_max is not None else None,
        "cpu_request": int(row["cpu_request"] or 0),
        "memory_request": int(row["memory_request"] or 0),
        "cpu_limit": _i(row["cpu_limit"]),
        "memory_limit": _i(row["memory_limit"]),
        "restart_count": _i(row["restart_count"]),
        "total_cost": float(row["total_cost"] or 0.0),
        "cost_cpu": _f(row["cost_cpu"]),
        "cost_ram": _f(row["cost_ram"]),
        "cost_storage": _f(row["cost_storage"]),
        "cost_network": _f(row["cost_network"]),
        "cost_lb": _f(row["cost_lb"]),
        "cost_source": cost_source,
        "co2e_grams": float(row["co2e_grams"] or 0.0),
        "embodied_co2e_grams": float(row["embodied_co2e_grams"] or 0.0),
        "joules": float(row["joules"] or 0.0),
        "gpu_power_watts": gpu_watts,
        "pue": float(row["pue_sum"] or 0.0) / pue_n if pue_n else 1.0,
        "grid_intensity": float(row["intensity_sum"] or 0.0) / intensity_n if intensity_n else 0.0,
        "emaps_zone": row["emaps_zone"],
        # Given explicitly: ``model_construct`` otherwise inspects the default
        # factory's signature for every row (a ``sys.modules`` copy each time
        # on Python 3.14), which inflates the scan's peak memory.
        "estimation_reasons": [],
    }
    if summary is not None:
        for resource, stats in (("cpu", summary.cpu), ("memory", summary.memory)):
            if stats is None:
                continue
            for p in USAGE_PERCENTILES:
                values[f"{resource}_usage_p{p}"] = stats.percentile(p)
    return BaselineMetric.model_construct(_fields_set=_BASELINE_FIELDS, **values)


def split_baseline(rows: Sequence[BaselineMetric]) -> Tuple[List[BaselineMetric], List[BaselineMetric]]:
    pods = [row for row in rows if not row.container_name]
    containers = [row for row in rows if row.container_name]
    return pods, containers


def hour_from_values(values: Mapping[str, Any], decode_time: Callable[[Any], Optional[datetime]]) -> WorkloadUsageHour:
    data = {column: values[column] for column in ROLLUP_COLUMNS}
    data["hour_bucket"] = decode_time(data["hour_bucket"])
    for column in ("sample_count", "pod_count", "step_count", "cpu_sample_count", "memory_sample_count"):
        data[column] = int(data[column] or 0)
    data["intensity_sample_count"] = int(data["intensity_sample_count"] or 0)
    data["hour_of_day"] = int(data["hour_of_day"])
    for column in ("cpu_request", "memory_request"):
        data[column] = int(data[column] or 0)
    for column in ("cpu_limit", "memory_limit", "restart_count"):
        data[column] = _i(data[column])
    data["latest_sample_offset_seconds"] = _f(data["latest_sample_offset_seconds"])
    data["restart_increase"] = _i(data["restart_increase"])
    return WorkloadUsageHour(**data)
