# src/greenkube/core/workload_usage.py
"""Hourly workload usage rollup and lookback summaries (roadmap E3, BUG-12).

Two pure functions define the rollup semantics:

* :func:`rollup_rows` aggregates the pod and container metric rows of whole
  hours into :class:`~greenkube.models.workload_usage.WorkloadUsageHour` rows,
  one per hour, target, container (``""`` = pod level) and node;
* :func:`summarize` aggregates hourly rows over a lookback into one
  :class:`~greenkube.models.workload_usage.WorkloadUsageSummary` per target and
  container. The SQL repositories implement the same aggregation in the
  database (``read_usage_snapshot``); this function is the reference used for
  scans built from in-memory rows (CLI ``--live``, tests).

Usage percentiles are per container and per replica: every pod row (or
container row) is one point, never a sum across replicas. Points in the
warm-up after a container (re)start (``RIGHTSIZING_WARMUP_MINUTES``) are left
out of the percentiles, so a startup spike (WAL replay, JIT, cache fill) does
not size a request; the hourly maximum keeps every point (USER BUG 2).

:class:`WorkloadUsageRollup` keeps the ``workload_usage_hourly`` table up to
date one hour at a time, so its memory is bounded by one hour of cluster data.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from greenkube.core.optimization.targeting import resolve_target
from greenkube.models.provenance import combine_cost_sources
from greenkube.models.workload_usage import (
    POD_LEVEL,
    USAGE_PERCENTILES,
    NodeUsageSummary,
    TargetKey,
    UsageSnapshot,
    UsageStats,
    WorkloadUsageHour,
    WorkloadUsageSummary,
)

if TYPE_CHECKING:
    from greenkube.storage.base_workload_usage_repository import WorkloadUsageRepository

logger = logging.getLogger(__name__)

#: Hours re-rolled on every compression run: raw rows of the last day may still
#: change (carbon-intensity recompute, late collection windows).
DEFAULT_REFRESH_HOURS = 24


# --------------------------------------------------------------------------- #
# Percentiles                                                                  #
# --------------------------------------------------------------------------- #


def nearest_rank_index(count: int, p: int) -> int:
    """0-based index of the nearest-rank ``p``-th percentile among ``count`` sorted values.

    Uses integer arithmetic (``ceil(p * count / 100)``) so SQL backends can
    compute the same rank: ``(p * count + 99) / 100``.
    """
    return max((p * count + 99) // 100, 1) - 1


def nearest_rank(values: Sequence[float], p: int) -> Optional[float]:
    """Nearest-rank percentile of unweighted values (None when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[nearest_rank_index(len(ordered), p)]


def weighted_nearest_rank(points: Sequence[Tuple[float, int]], p: int) -> Optional[float]:
    """Nearest-rank percentile of ``(value, weight)`` points: the smallest value covering ``p``% of the weight."""
    if not points:
        return None
    ordered = sorted(points)
    total = sum(weight for _, weight in ordered)
    threshold = nearest_rank_index(total, p) + 1
    running = 0
    for value, weight in ordered:
        running += weight
        if running >= threshold:
            return value
    return ordered[-1][0]


# --------------------------------------------------------------------------- #
# Hourly rollup                                                                #
# --------------------------------------------------------------------------- #


def floor_hour(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.replace(minute=0, second=0, microsecond=0)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weight(row: Any) -> int:
    value = getattr(row, "sample_count", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _source_aggregate(values: List[Optional[str]]) -> Optional[str]:
    """One cost provenance value for a group: shared value, ``mixed``, or None.

    ``opencost`` mixed only with ``opencost_derived`` stays trusted
    (:func:`~greenkube.models.provenance.combine_cost_sources`).
    """
    return combine_cost_sources(values)


def _mode(values: List[Optional[str]]) -> Optional[str]:
    counts: Dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            counts[value] += 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _sum_optional(total: Optional[float], value: Any) -> Optional[float]:
    number = _number(value)
    if number is None:
        return total
    return number if total is None else total + number


@dataclass
class _SyntheticPodRow:
    """Pod-level row rebuilt from the container rows of a pod without a pod row."""

    namespace: str
    pod_name: str
    timestamp: Optional[datetime]
    owner_kind: Optional[str] = None
    owner_name: Optional[str] = None
    node: Optional[str] = None
    sample_count: int = 1
    duration_seconds: Optional[float] = None
    cpu_usage_millicores: Optional[float] = None
    cpu_usage_max_millicores: Optional[float] = None
    memory_usage_bytes: Optional[float] = None
    memory_usage_max_bytes: Optional[float] = None
    cpu_request: float = 0
    memory_request: float = 0
    restart_count: Optional[int] = None
    total_cost: float = 0.0
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_storage: Optional[float] = None
    cost_network: Optional[float] = None
    cost_lb: Optional[float] = None
    cost_sources: List[Optional[str]] = field(default_factory=list)
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    joules: float = 0.0
    gpu_power_watts: Optional[float] = None
    pue: Optional[float] = None
    grid_intensity: Optional[float] = None
    emaps_zone: Optional[str] = None

    @property
    def cost_source(self) -> Optional[str]:
        return _source_aggregate(self.cost_sources)

    def add(self, row: Any) -> None:
        self.sample_count = max(self.sample_count, _weight(row))
        duration = _number(getattr(row, "duration_seconds", None))
        if duration is not None:
            self.duration_seconds = max(self.duration_seconds or 0.0, duration)
        self.cpu_usage_millicores = _sum_optional(self.cpu_usage_millicores, getattr(row, "cpu_usage_millicores", None))
        self.memory_usage_bytes = _sum_optional(self.memory_usage_bytes, getattr(row, "memory_usage_bytes", None))
        cpu_peak = _number(getattr(row, "cpu_usage_max_millicores", None))
        if cpu_peak is not None:
            self.cpu_usage_max_millicores = (self.cpu_usage_max_millicores or 0.0) + cpu_peak
        memory_peak = _number(getattr(row, "memory_usage_max_bytes", None))
        if memory_peak is not None:
            self.memory_usage_max_bytes = (self.memory_usage_max_bytes or 0.0) + memory_peak
        self.cpu_request += _number(getattr(row, "cpu_request", 0)) or 0
        self.memory_request += _number(getattr(row, "memory_request", 0)) or 0
        self.total_cost += _number(getattr(row, "total_cost", 0)) or 0.0
        for name in ("cost_cpu", "cost_ram", "cost_storage", "cost_network", "cost_lb"):
            setattr(self, name, _sum_optional(getattr(self, name), getattr(row, name, None)))
        self.cost_sources.append(getattr(row, "cost_source", None))
        self.co2e_grams += _number(getattr(row, "co2e_grams", 0)) or 0.0
        self.embodied_co2e_grams += _number(getattr(row, "embodied_co2e_grams", 0)) or 0.0
        self.joules += _number(getattr(row, "joules", 0)) or 0.0
        self.gpu_power_watts = _sum_optional(self.gpu_power_watts, getattr(row, "gpu_power_watts", None))
        self.pue = self.pue or _number(getattr(row, "pue", None))
        self.grid_intensity = self.grid_intensity or _number(getattr(row, "grid_intensity", None))
        self.emaps_zone = self.emaps_zone or getattr(row, "emaps_zone", None)


@dataclass
class _HourAccumulator:
    pods: set = field(default_factory=set)
    #: Latest sample time of each pod of the hour (replicas at the latest step, F-08).
    pod_latest: Dict[Any, float] = field(default_factory=dict)
    step_seconds: float = 0.0
    steps: Dict[Any, int] = field(default_factory=dict)
    sample_count: int = 0
    duration_seconds: float = 0.0
    cpu: List[Tuple[float, int]] = field(default_factory=list)
    #: Points of the warm-up after a (re)start: out of the percentiles, kept in the maximum.
    cpu_warm: List[Tuple[float, int]] = field(default_factory=list)
    cpu_peak: Optional[float] = None
    cpu_sum: float = 0.0
    cpu_sq_sum: float = 0.0
    cpu_weight: int = 0
    memory: List[Tuple[float, int]] = field(default_factory=list)
    memory_warm: List[Tuple[float, int]] = field(default_factory=list)
    memory_peak: Optional[float] = None
    memory_sum: float = 0.0
    memory_weight: int = 0
    cpu_request_sum: float = 0.0
    memory_request_sum: float = 0.0
    latest: Any = None
    latest_rows: List[Any] = field(default_factory=list)
    restart_count: Optional[int] = None
    #: Sum of the per-pod restart increases of the hour's rows (:func:`restart_deltas`).
    restart_increase: Optional[int] = None
    total_cost: float = 0.0
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_storage: Optional[float] = None
    cost_network: Optional[float] = None
    cost_lb: Optional[float] = None
    cost_sources: List[Optional[str]] = field(default_factory=list)
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    joules: float = 0.0
    gpu_watt_seconds: Optional[float] = None
    pue_sum: float = 0.0
    pue_weight: int = 0
    intensity_sum: float = 0.0
    intensity_weight: int = 0
    zones: List[Optional[str]] = field(default_factory=list)
    instance_types: List[Optional[str]] = field(default_factory=list)

    def add(self, row: Any, step_seconds: float, warm: bool = False, restart_delta: Optional[int] = None) -> None:
        weight = _weight(row)
        timestamp = getattr(row, "timestamp", None)
        pod_name = getattr(row, "pod_name", None)
        self.pods.add(pod_name)
        self.step_seconds = step_seconds
        if isinstance(timestamp, datetime):
            stamp = timestamp.timestamp()
            self.pod_latest[pod_name] = max(self.pod_latest.get(pod_name, stamp), stamp)
        self.steps[timestamp] = max(self.steps.get(timestamp, 0), weight)
        self.sample_count += weight
        duration = _number(getattr(row, "duration_seconds", None))
        seconds = duration if duration is not None and duration > 0 else step_seconds * weight
        self.duration_seconds += seconds
        gpu_watts = _number(getattr(row, "gpu_power_watts", None))
        if gpu_watts is not None:
            self.gpu_watt_seconds = (self.gpu_watt_seconds or 0.0) + max(gpu_watts, 0.0) * seconds

        # A compressed hourly row stands for many samples: its point is the hour's
        # peak. When it covers a warm-up, its average stands for the hour instead
        # (the peak is the warm-up's), and the peak only reaches the maximum.
        hour_average = warm and weight > 1
        cpu = _number(getattr(row, "cpu_usage_millicores", None))
        if cpu is not None:
            peak = _number(getattr(row, "cpu_usage_max_millicores", None))
            point = max(cpu, peak) if peak is not None else cpu
            (self.cpu_warm if warm else self.cpu).append((cpu if hour_average else point, weight))
            self.cpu_peak = point if self.cpu_peak is None else max(self.cpu_peak, point)
            self.cpu_sum += cpu * weight
            self.cpu_sq_sum += cpu * cpu * weight
            self.cpu_weight += weight
        memory = _number(getattr(row, "memory_usage_bytes", None))
        if memory is not None:
            peak = _number(getattr(row, "memory_usage_max_bytes", None))
            point = max(memory, peak) if peak is not None else memory
            (self.memory_warm if warm else self.memory).append((memory if hour_average else point, weight))
            self.memory_peak = point if self.memory_peak is None else max(self.memory_peak, point)
            self.memory_sum += memory * weight
            self.memory_weight += weight

        self.cpu_request_sum += (_number(getattr(row, "cpu_request", 0)) or 0.0) * weight
        self.memory_request_sum += (_number(getattr(row, "memory_request", 0)) or 0.0) * weight

        order = timestamp.timestamp() if isinstance(timestamp, datetime) else float("-inf")
        if self.latest is None or order > self.latest:
            self.latest = order
            self.latest_rows = [row]
        elif order == self.latest:
            self.latest_rows.append(row)

        restarts = getattr(row, "restart_count", None)
        if isinstance(restarts, int) and not isinstance(restarts, bool):
            self.restart_count = restarts if self.restart_count is None else max(self.restart_count, restarts)
        if restart_delta is not None:
            self.restart_increase = (self.restart_increase or 0) + restart_delta
        self.total_cost += _number(getattr(row, "total_cost", 0)) or 0.0
        for name in ("cost_cpu", "cost_ram", "cost_storage", "cost_network", "cost_lb"):
            setattr(self, name, _sum_optional(getattr(self, name), getattr(row, name, None)))
        self.cost_sources.append(getattr(row, "cost_source", None))
        self.co2e_grams += _number(getattr(row, "co2e_grams", 0)) or 0.0
        self.embodied_co2e_grams += _number(getattr(row, "embodied_co2e_grams", 0)) or 0.0
        self.joules += _number(getattr(row, "joules", 0)) or 0.0
        pue = _number(getattr(row, "pue", None))
        if pue is not None and pue > 0:
            self.pue_sum += pue * weight
            self.pue_weight += weight
        intensity = _number(getattr(row, "grid_intensity", None))
        if intensity is not None and intensity > 0:
            self.intensity_sum += intensity * weight
            self.intensity_weight += weight
        self.zones.append(getattr(row, "emaps_zone", None))
        self.instance_types.append(getattr(row, "node_instance_type", None))

    def _latest_pod_count(self) -> int:
        """Pods whose latest sample is within half a step of the hour's latest sample (F-08).

        Mirrors ``_current_replicas`` on the rows path: a pod a rollout replaced
        earlier in the hour is not a replica of the latest step.
        """
        if not self.pod_latest:
            return len(self.pods)
        latest = max(self.pod_latest.values())
        tolerance = max(self.step_seconds, 0.0) / 2
        return sum(1 for seen in self.pod_latest.values() if latest - seen <= tolerance) or len(self.pods)

    def _latest(self, name: str, *, optional: bool) -> Optional[int]:
        values = [_number(getattr(row, name, None)) for row in self.latest_rows]
        present = [value for value in values if value is not None]
        if not present:
            return None if optional else 0
        return int(max(present))

    def build(self, bucket: Optional[datetime], key: Tuple[str, str, str, str, str], source: str) -> WorkloadUsageHour:
        namespace, kind, name, container, node = key
        # An hour that is all warm-up keeps its points rather than having no percentile.
        cpu_points = self.cpu or self.cpu_warm
        memory_points = self.memory or self.memory_warm
        cpu_values: Dict[int, Optional[float]] = {p: weighted_nearest_rank(cpu_points, p) for p in USAGE_PERCENTILES}
        memory_values: Dict[int, Optional[float]] = {
            p: weighted_nearest_rank(memory_points, p) for p in USAGE_PERCENTILES
        }
        cpu_values[100] = self.cpu_peak
        memory_values[100] = self.memory_peak
        return WorkloadUsageHour(
            hour_bucket=bucket,
            namespace=namespace,
            target_kind=kind,
            target_name=name,
            container_name=container,
            node=node,
            hour_of_day=bucket.hour if bucket is not None else None,
            sample_count=self.sample_count,
            pod_count=len(self.pods),
            latest_pod_count=self._latest_pod_count(),
            step_count=sum(self.steps.values()),
            duration_seconds=self.duration_seconds,
            cpu_sample_count=self.cpu_weight,
            cpu_usage_avg=self.cpu_sum / self.cpu_weight if self.cpu_weight else None,
            cpu_usage_sq_avg=self.cpu_sq_sum / self.cpu_weight if self.cpu_weight else None,
            cpu_usage_p50=cpu_values[50],
            cpu_usage_p90=cpu_values[90],
            cpu_usage_p95=cpu_values[95],
            cpu_usage_p99=cpu_values[99],
            cpu_usage_max=cpu_values[100],
            memory_sample_count=self.memory_weight,
            memory_usage_avg=self.memory_sum / self.memory_weight if self.memory_weight else None,
            memory_usage_p50=memory_values[50],
            memory_usage_p90=memory_values[90],
            memory_usage_p95=memory_values[95],
            memory_usage_p99=memory_values[99],
            memory_usage_max=memory_values[100],
            cpu_request=self._latest("cpu_request", optional=False) or 0,
            memory_request=self._latest("memory_request", optional=False) or 0,
            cpu_request_sum=self.cpu_request_sum,
            memory_request_sum=self.memory_request_sum,
            cpu_limit=self._latest("cpu_limit", optional=True),
            memory_limit=self._latest("memory_limit", optional=True),
            restart_count=self.restart_count,
            restart_increase=self.restart_increase,
            total_cost=self.total_cost,
            cost_cpu=self.cost_cpu,
            cost_ram=self.cost_ram,
            cost_storage=self.cost_storage,
            cost_network=self.cost_network,
            cost_lb=self.cost_lb,
            cost_source=_source_aggregate(self.cost_sources),
            co2e_grams=self.co2e_grams,
            embodied_co2e_grams=self.embodied_co2e_grams,
            joules=self.joules,
            gpu_watt_seconds=self.gpu_watt_seconds,
            pue=self.pue_sum / self.pue_weight if self.pue_weight else None,
            grid_intensity=self.intensity_sum / self.intensity_weight if self.intensity_weight else None,
            intensity_sample_count=self.intensity_weight,
            emaps_zone=_mode(self.zones),
            node_instance_type=_mode(self.instance_types),
            source=source,
            latest_sample_offset_seconds=(
                max(self.latest - bucket.timestamp(), 0.0)
                if bucket is not None and self.latest is not None and math.isfinite(self.latest)
                else None
            ),
        )


def _bucket(row: Any) -> Optional[datetime]:
    timestamp = getattr(row, "timestamp", None)
    return floor_hour(timestamp) if isinstance(timestamp, datetime) else None


WarmupWindows = Dict[Tuple[str, str], List[Tuple[datetime, datetime]]]


def warmup_windows(rows: Iterable[Any], warmup_seconds: float) -> WarmupWindows:
    """Warm-up windows ``[start, start + warmup)`` of each pod, from its pod rows.

    A window opens at the first sample of a pod whose restart count changed
    since its previous sample (a container restarted, or a pod was recreated
    under the same name), and at the first sample of a pod that appears after
    collection had started (a new replica). Rows only need ``namespace``,
    ``pod_name``, ``timestamp`` and ``restart_count``.
    """
    if warmup_seconds <= 0:
        return {}
    samples: Dict[Tuple[str, str], List[Tuple[datetime, Optional[int]]]] = defaultdict(list)
    first: Optional[datetime] = None
    for row in rows:
        timestamp = getattr(row, "timestamp", None)
        if not isinstance(timestamp, datetime):
            continue
        restarts = getattr(row, "restart_count", None)
        count = restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else None
        samples[(row.namespace, row.pod_name)].append((timestamp, count))
        first = timestamp if first is None or timestamp < first else first
    span = timedelta(seconds=warmup_seconds)
    windows: WarmupWindows = {}
    for pod, points in samples.items():
        points.sort(key=lambda point: point[0])
        previous: Optional[int] = None
        for index, (timestamp, count) in enumerate(points):
            appeared = index == 0 and first is not None and timestamp > first
            restarted = count is not None and previous is not None and count != previous
            if appeared or restarted:
                windows.setdefault(pod, []).append((timestamp, timestamp + span))
            if count is not None:
                previous = count
    return windows


def _restart_value(row: Any) -> Optional[int]:
    restarts = getattr(row, "restart_count", None)
    return restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else None


def restart_deltas(
    rows: Iterable[Any],
    context_rows: Iterable[Any] = (),
    *,
    per_container: bool = False,
    collection_started: bool = False,
) -> Dict[int, int]:
    """Restart increase of each row since the previous known count of the same pod (REC-04).

    Returns ``{id(row): increase}`` for the ``rows`` that carry a restart count.
    Counts are compared per pod (``namespace``, ``pod_name``, and the container
    with ``per_container``), never across replicas: the highest count of
    several replicas is not a counter. For each row, oldest first:

    * a rise since the pod's previous known count adds the difference;
    * a drop is a counter reset (the pod was recreated under the same name):
      it adds the new count;
    * a pod without any previous known count adds its whole count when
      collection had already started (``collection_started``: older stored
      rows exist, or an earlier sample of any pod is among the rows): a new pod
      starts at zero, and a pod whose previous count is unknown is not known to
      be clean. At the very first sample, the count is the baseline (0 added).

    ``context_rows`` give previous counts and are not returned: the rows just
    before ``rows`` and, for pods without a count there, each pod's last stored
    count however old (a collection gap or a missing hour never hides the
    restarts that happened in it). Overcounting never allows a
    decrease, so every unknown counts as restarts (fail closed).
    """
    rows = list(rows)

    def key(row: Any) -> Tuple[Any, ...]:
        base = (getattr(row, "namespace", None), getattr(row, "pod_name", None))
        return (*base, getattr(row, "container_name", None)) if per_container else base

    def order(row: Any) -> float:
        # Rows without a timestamp (in-memory rows) sort first, in input order.
        timestamp = getattr(row, "timestamp", None)
        return timestamp.timestamp() if isinstance(timestamp, datetime) else float("-inf")

    stamped = [(row, False) for row in context_rows] + [(row, True) for row in rows]
    if not stamped:
        return {}
    first = min(order(item[0]) for item in stamped)
    stamped.sort(key=lambda item: (order(item[0]), item[1]))
    last: Dict[Tuple[Any, ...], int] = {}
    deltas: Dict[int, int] = {}
    for row, own in stamped:
        count = _restart_value(row)
        if count is None:
            continue
        pod = key(row)
        previous = last.get(pod)
        if previous is not None:
            delta = count - previous if count >= previous else count
        elif collection_started or order(row) > first:
            delta = count
        else:
            delta = 0
        last[pod] = count
        if own:
            deltas[id(row)] = delta
    return deltas


def _in_warmup(windows: WarmupWindows, row: Any) -> bool:
    spans = windows.get((row.namespace, row.pod_name))
    timestamp = getattr(row, "timestamp", None)
    if not spans or not isinstance(timestamp, datetime):
        return False
    return any(start <= timestamp < end for start, end in spans)


def rollup_rows(
    pod_rows: Iterable[Any],
    container_rows: Iterable[Any] = (),
    *,
    step_seconds: float = 300.0,
    source: str = "raw",
    warmup_seconds: float = 0.0,
    context_rows: Iterable[Any] = (),
    restart_context_rows: Optional[Iterable[Any]] = None,
    collection_started: bool = False,
) -> List[WorkloadUsageHour]:
    """Aggregates metric rows into hourly rows per target, container and node.

    ``pod_rows`` are pod-level rows (``combined_metrics`` or its hourly table);
    ``container_rows`` are container-level rows. Rows are duck-typed: any
    object with the ``CombinedMetric`` attribute names works. A pod that has
    container rows but no pod row at a timestamp gets a pod-level row rebuilt
    from its containers, so every pod has a pod-level aggregate.

    With ``warmup_seconds``, the points of a pod's warm-up (see
    :func:`warmup_windows`) are left out of the usage percentiles;
    ``context_rows`` are the raw pod rows just before the rolled rows, so a
    restart shortly before the hour is still seen. Rows from the compressed
    hourly tables (one row per pod and hour, ``restart_count`` the hour's
    maximum) take the previous hour's compressed rows as context: a pod whose
    restart count rose since then, or that was absent from it, warmed up in the
    hour, so its row leaves the percentiles when other points remain and
    otherwise counts with its hourly average instead of its peak.
    """
    pod_rows = list(pod_rows)
    container_rows = list(container_rows)
    context_rows = list(context_rows)
    warm = warmup_windows([*context_rows, *pod_rows], warmup_seconds)
    # Restart increases compare each pod with its last known count (``restart_context_rows``,
    # by default the warm-up context); ``collection_started``: older stored rows exist.
    restart_context = context_rows if restart_context_rows is None else list(restart_context_rows)
    deltas = restart_deltas(pod_rows, restart_context, collection_started=collection_started)
    deltas.update(restart_deltas(container_rows, per_container=True))
    pods_at: Dict[Tuple[str, str, Any], Any] = {}
    pods_in_hour: Dict[Tuple[str, str, Optional[datetime]], Any] = {}
    for row in pod_rows:
        pods_at[(row.namespace, row.pod_name, getattr(row, "timestamp", None))] = row
        pods_in_hour.setdefault((row.namespace, row.pod_name, _bucket(row)), row)

    synthetic: Dict[Tuple[str, str, Any], _SyntheticPodRow] = {}
    for row in container_rows:
        key = (row.namespace, row.pod_name, getattr(row, "timestamp", None))
        if key in pods_at:
            continue
        pod = synthetic.get(key)
        if pod is None:
            pod = _SyntheticPodRow(
                namespace=row.namespace,
                pod_name=row.pod_name,
                timestamp=key[2],
                owner_kind=getattr(row, "owner_kind", None),
                owner_name=getattr(row, "owner_name", None),
                sample_count=_weight(row),
            )
            synthetic[key] = pod
        pod.add(row)

    groups: Dict[Tuple[Optional[datetime], Tuple[str, str, str, str, str]], _HourAccumulator] = {}

    def _group(bucket: Optional[datetime], key: Tuple[str, str, str, str, str]) -> _HourAccumulator:
        accumulator = groups.get((bucket, key))
        if accumulator is None:
            accumulator = _HourAccumulator()
            groups[(bucket, key)] = accumulator
        return accumulator

    # A pod's workload owner, from any of its rows: pod rows written before
    # owners were stored on them would otherwise make every StatefulSet replica
    # its own target, and its containers with it.
    owners: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for row in [*pod_rows, *container_rows]:
        owner_kind, owner_name = getattr(row, "owner_kind", None), getattr(row, "owner_name", None)
        if owner_kind and owner_name:
            owners.setdefault((row.namespace, row.pod_name), (owner_kind, owner_name))

    def _target(row: Any) -> Tuple[str, str, str]:
        owner = owners.get((row.namespace, row.pod_name))
        return resolve_target(row.namespace, row.pod_name, *(owner or (None, None)))

    for row in [*pod_rows, *synthetic.values()]:
        namespace, kind, name = _target(row)
        node = getattr(row, "node", None) or ""
        _group(_bucket(row), (namespace, kind, name, POD_LEVEL, node)).add(
            row, step_seconds, _in_warmup(warm, row), deltas.get(id(row))
        )

    for row in container_rows:
        container = getattr(row, "container_name", None)
        if not container or container.strip() != container:
            continue
        timestamp = getattr(row, "timestamp", None)
        bucket = _bucket(row)
        pod = pods_at.get((row.namespace, row.pod_name, timestamp)) or pods_in_hour.get(
            (row.namespace, row.pod_name, bucket)
        )
        namespace, kind, name = _target(row)
        node = (getattr(pod, "node", None) if pod is not None else None) or getattr(row, "node", None) or ""
        _group(bucket, (namespace, kind, name, container, node)).add(
            row, step_seconds, _in_warmup(warm, row), deltas.get(id(row))
        )

    return [accumulator.build(bucket, key, source) for (bucket, key), accumulator in groups.items()]


# --------------------------------------------------------------------------- #
# Lookback summaries (reference implementation of the SQL aggregation)        #
# --------------------------------------------------------------------------- #


@dataclass
class _HourTotals:
    """One hour of a target container, all nodes merged (the SQL ``hourly`` CTE)."""

    hour_bucket: Optional[datetime]
    sample_count: int = 0
    step_count: int = 0
    pod_count: int = 0
    duration_seconds: float = 0.0
    cpu_n: int = 0
    cpu_sum: float = 0.0
    cpu_sq_sum: float = 0.0
    cpu_p: Dict[int, Optional[float]] = field(default_factory=dict)
    memory_n: int = 0
    memory_sum: float = 0.0
    memory_p: Dict[int, Optional[float]] = field(default_factory=dict)
    cpu_request: int = 0
    memory_request: int = 0
    cpu_limit: Optional[int] = None
    memory_limit: Optional[int] = None
    #: Latest-sample offset of the node rows whose requests and limits are kept.
    latest_offset: Optional[float] = None
    #: ``(latest-sample offset, pods at that sample)`` of each node row (F-08).
    latest_pods: List[Tuple[float, int]] = field(default_factory=list)
    restart_count: Optional[int] = None
    #: Sum of the node rows' per-pod restart increases (None when no row carries one).
    restart_increase: Optional[int] = None
    #: A node row carries a restart count but no increase (rolled before migration 0130).
    restart_increase_unknown: bool = False
    total_cost: float = 0.0
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    joules: float = 0.0
    intensity_sum: float = 0.0
    intensity_n: int = 0
    emaps_zone: Optional[str] = None


def latest_step_tolerance_seconds() -> int:
    """Half the configured sampling step: node rows of one hour whose latest samples are this close
    to the hour's newest are the same step (the SQL summary inlines the same value, F-08)."""
    from greenkube.core.config import get_config
    from greenkube.utils.time import parse_query_step_seconds

    try:
        step = parse_query_step_seconds(str(getattr(get_config(), "PROMETHEUS_QUERY_RANGE_STEP", "") or "5m"))
    except Exception:
        step = 300
    return max(int(step) // 2, 0)


def _replicas_at_latest_step(totals: "_HourTotals") -> int:
    """Pods of the hour's latest step, all nodes merged: node rows whose latest sample is within
    half a step of the newest one (a pod a rollout moved to another node earlier in the hour is
    not counted)."""
    if not totals.latest_pods:
        return totals.pod_count
    newest = max(offset for offset, _ in totals.latest_pods)
    tolerance = latest_step_tolerance_seconds()
    return sum(count for offset, count in totals.latest_pods if newest - offset <= tolerance)


def _max_optional(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _merge_hour(totals: _HourTotals, hour: WorkloadUsageHour) -> None:
    totals.sample_count += hour.sample_count
    totals.step_count = max(totals.step_count, hour.step_count)
    totals.pod_count += hour.pod_count
    totals.duration_seconds += hour.duration_seconds
    if hour.cpu_usage_avg is not None and hour.cpu_sample_count:
        totals.cpu_n += hour.cpu_sample_count
        totals.cpu_sum += hour.cpu_usage_avg * hour.cpu_sample_count
        totals.cpu_sq_sum += (hour.cpu_usage_sq_avg or 0.0) * hour.cpu_sample_count
    if hour.memory_usage_avg is not None and hour.memory_sample_count:
        totals.memory_n += hour.memory_sample_count
        totals.memory_sum += hour.memory_usage_avg * hour.memory_sample_count
    for p in (*USAGE_PERCENTILES, 100):
        attr = "max" if p == 100 else f"p{p}"
        totals.cpu_p[p] = _max_optional(totals.cpu_p.get(p), getattr(hour, f"cpu_usage_{attr}"))
        totals.memory_p[p] = _max_optional(totals.memory_p.get(p), getattr(hour, f"memory_usage_{attr}"))
    # Requests and limits come from the node row with the newest sample (a pod
    # moved to another node by a rollout), the highest on a tie or without offsets.
    offset = hour.latest_sample_offset_seconds if hour.latest_sample_offset_seconds is not None else -1.0
    pods_at_latest = hour.latest_pod_count if hour.latest_pod_count is not None else hour.pod_count
    totals.latest_pods.append((offset, pods_at_latest))
    if totals.latest_offset is None or offset > totals.latest_offset:
        totals.latest_offset = offset
        totals.cpu_request = hour.cpu_request or 0
        totals.memory_request = hour.memory_request or 0
        totals.cpu_limit = hour.cpu_limit
        totals.memory_limit = hour.memory_limit
    elif offset == totals.latest_offset:
        totals.cpu_request = max(totals.cpu_request, hour.cpu_request or 0)
        totals.memory_request = max(totals.memory_request, hour.memory_request or 0)
        totals.cpu_limit = _max_int(totals.cpu_limit, hour.cpu_limit)
        totals.memory_limit = _max_int(totals.memory_limit, hour.memory_limit)
    totals.restart_count = _max_int(totals.restart_count, hour.restart_count)
    if hour.restart_increase is not None:
        totals.restart_increase = (totals.restart_increase or 0) + hour.restart_increase
    elif hour.restart_count is not None:
        totals.restart_increase_unknown = True
    totals.total_cost += hour.total_cost or 0.0
    totals.co2e_grams += hour.co2e_grams or 0.0
    totals.embodied_co2e_grams += hour.embodied_co2e_grams or 0.0
    totals.joules += hour.joules or 0.0
    if hour.grid_intensity is not None and hour.intensity_sample_count:
        totals.intensity_sum += hour.grid_intensity * hour.intensity_sample_count
        totals.intensity_n += hour.intensity_sample_count
    if hour.emaps_zone and (totals.emaps_zone is None or hour.emaps_zone > totals.emaps_zone):
        totals.emaps_zone = hour.emaps_zone


def _max_int(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _stats(
    n: int,
    total: float,
    sq_total: Optional[float],
    hourly: List[Dict[int, Optional[float]]],
) -> Optional[UsageStats]:
    if n <= 0:
        return None
    avg = total / n
    values: Dict[int, float] = {}
    for p in USAGE_PERCENTILES:
        present = [value for hour in hourly if (value := hour.get(p)) is not None]
        picked = nearest_rank(present, p)
        values[p] = picked if picked is not None else avg
    maxima = [value for hour in hourly if (value := hour.get(100)) is not None]
    observed_max = max(maxima) if maxima else avg
    stddev = None
    if sq_total is not None:
        variance = max(sq_total / n - avg * avg, 0.0)
        stddev = variance**0.5
    return UsageStats(
        sample_count=n,
        avg=avg,
        p50=values[50],
        p90=values[90],
        p95=values[95],
        p99=values[99],
        max=float(observed_max),
        stddev=stddev,
    )


def _sort_key(bucket: Optional[datetime]) -> float:
    return bucket.timestamp() if bucket is not None else float("-inf")


def summarize(hours: Iterable[WorkloadUsageHour], *, source: str = "rows") -> UsageSnapshot:
    """Aggregates hourly rows into the usage snapshot analyzers read."""
    per_hour: Dict[Tuple[TargetKey, str], Dict[Optional[datetime], _HourTotals]] = defaultdict(dict)
    profile_sum: Dict[TargetKey, List[float]] = defaultdict(lambda: [0.0] * 24)
    profile_n: Dict[TargetKey, List[int]] = defaultdict(lambda: [0] * 24)
    node_hours: Dict[Tuple[str, Optional[datetime]], List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    first_hour: Optional[datetime] = None

    for hour in hours:
        key = (hour.target, hour.container_name)
        totals = per_hour[key].get(hour.hour_bucket)
        if totals is None:
            totals = _HourTotals(hour_bucket=hour.hour_bucket)
            per_hour[key][hour.hour_bucket] = totals
        _merge_hour(totals, hour)
        if hour.hour_bucket is not None and (first_hour is None or hour.hour_bucket < first_hour):
            first_hour = hour.hour_bucket
        if hour.container_name != POD_LEVEL:
            continue
        if hour.hour_of_day is not None and hour.cpu_usage_avg is not None and hour.cpu_sample_count:
            profile_sum[hour.target][hour.hour_of_day] += hour.cpu_usage_avg * hour.cpu_sample_count
            profile_n[hour.target][hour.hour_of_day] += hour.cpu_sample_count
        if hour.node:
            acc = node_hours[(hour.node, hour.hour_bucket)]
            acc[0] = max(acc[0], hour.step_count)
            acc[1] += (hour.cpu_usage_avg or 0.0) * hour.cpu_sample_count
            acc[2] += (hour.memory_usage_avg or 0.0) * hour.memory_sample_count
            acc[3] += hour.cpu_request_sum
            acc[4] += hour.memory_request_sum

    snapshot = UsageSnapshot(source=source, first_hour=first_hour)
    for (target, container), hourly in per_hour.items():
        ordered = sorted(hourly.values(), key=lambda item: _sort_key(item.hour_bucket))
        last = ordered[-1]
        buckets = [item.hour_bucket for item in ordered if item.hour_bucket is not None]
        intensity_n = sum(item.intensity_n for item in ordered)
        summary = WorkloadUsageSummary(
            namespace=target[0],
            target_kind=target[1],
            target_name=target[2],
            container_name=container,
            first_hour=buckets[0] if buckets else None,
            last_hour=buckets[-1] if buckets else None,
            hours_observed=len(ordered),
            sample_count=sum(item.sample_count for item in ordered),
            step_count=sum(item.step_count for item in ordered),
            duration_seconds=sum(item.duration_seconds for item in ordered),
            replicas_max=max(item.pod_count for item in ordered),
            replicas_latest=_replicas_at_latest_step(ordered[-1]),
            cpu=_stats(
                sum(item.cpu_n for item in ordered),
                sum(item.cpu_sum for item in ordered),
                sum(item.cpu_sq_sum for item in ordered),
                [item.cpu_p for item in ordered if item.cpu_n],
            ),
            memory=_stats(
                sum(item.memory_n for item in ordered),
                sum(item.memory_sum for item in ordered),
                None,
                [item.memory_p for item in ordered if item.memory_n],
            ),
            cpu_request=last.cpu_request,
            memory_request=last.memory_request,
            cpu_limit=last.cpu_limit,
            memory_limit=last.memory_limit,
            restart_count=_restart_max(ordered),
            restart_increase=_restart_increase(ordered),
            restart_increase_legacy=any(item.restart_increase_unknown for item in ordered),
            memory_config_since=_config_since(ordered, ("memory_request", "memory_limit")),
            resources_config_since=_config_since(ordered, _RESOURCE_FIELDS),
            total_cost=sum(item.total_cost for item in ordered),
            co2e_grams=sum(item.co2e_grams for item in ordered),
            embodied_co2e_grams=sum(item.embodied_co2e_grams for item in ordered),
            joules=sum(item.joules for item in ordered),
            grid_intensity=(sum(item.intensity_sum for item in ordered) / intensity_n) if intensity_n else None,
            intensity_sample_count=intensity_n,
            emaps_zone=max((item.emaps_zone for item in ordered if item.emaps_zone), default=None),
        )
        snapshot.summaries.setdefault(target, {})[container] = summary

    for target, sums in profile_sum.items():
        counts = profile_n[target]
        snapshot.hour_profiles[target] = tuple(sums[h] / counts[h] if counts[h] else None for h in range(24))
        snapshot.hour_profile_samples[target] = sum(counts)

    per_node: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    for (node, _bucket_value), acc in node_hours.items():
        totals = per_node[node]
        totals[0] += 1
        for index in range(5):
            totals[index + 1] += acc[index]
    for node, totals in per_node.items():
        steps = totals[1]
        snapshot.nodes[node] = NodeUsageSummary(
            node=node,
            hours_observed=int(totals[0]),
            cpu_usage_avg=totals[2] / steps if steps else 0.0,
            memory_usage_avg=totals[3] / steps if steps else 0.0,
            cpu_request_avg=totals[4] / steps if steps else 0.0,
            memory_request_avg=totals[5] / steps if steps else 0.0,
        )
    return snapshot


#: Request and limit fields of an hour: any change is a new pod template (REC-02).
_RESOURCE_FIELDS = ("cpu_request", "memory_request", "cpu_limit", "memory_limit")


def _config_since(ordered: List[_HourTotals], fields: Sequence[str]) -> Optional[datetime]:
    """Start of the last hour whose ``fields`` differ from the latest hour's, or None (REC-02).

    Kills and restarts at or after it may have happened under the current
    configuration: during a rollout the hour still shows the old values (the
    rollup keeps the latest sample's), so a kill under the new limit in that
    hour counts.
    Hours without a known request (0) are skipped: they say nothing about it.
    Same rule as ``memory_changed_hour`` / ``resources_changed_hour`` of the SQL summary.
    """
    if not ordered:
        return None
    latest = tuple(getattr(ordered[-1], name) for name in fields)
    for item in reversed(ordered[:-1]):
        if not item.memory_request and not item.cpu_request:
            continue
        if tuple(getattr(item, name) for name in fields) != latest and item.hour_bucket is not None:
            return _hour_start(item.hour_bucket)
    return None


def _hour_start(bucket: Any) -> datetime:
    """An hour bucket as an aware datetime (datetime or ISO string)."""
    value = bucket if isinstance(bucket, datetime) else datetime.fromisoformat(str(bucket).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _restart_increase(ordered: List[_HourTotals]) -> Optional[int]:
    """Restarts over the lookback: the sum of the hours' per-pod restart increases.

    None when no hour carries a restart count, or when an hour carries a count
    without an increase (rolled before migration 0130): unknown, never zero.
    Hours without a count are skipped, as in the SQL summary.
    """
    counted = [item for item in ordered if item.restart_count is not None or item.restart_increase is not None]
    if not counted or any(item.restart_increase_unknown for item in counted):
        return None
    return sum(item.restart_increase or 0 for item in counted)


def _restart_max(ordered: List[_HourTotals]) -> Optional[int]:
    values = [item.restart_count for item in ordered if item.restart_count is not None]
    return max(values) if values else None


def summarize_rows(
    pod_rows: Iterable[Any],
    container_rows: Iterable[Any] = (),
    *,
    step_seconds: float = 300.0,
    warmup_seconds: float = 0.0,
) -> UsageSnapshot:
    """Usage snapshot of in-memory metric rows (CLI ``--live`` and contexts built from rows)."""
    hours = rollup_rows(pod_rows, container_rows, step_seconds=step_seconds, warmup_seconds=warmup_seconds)
    return summarize(hours, source="rows")


# --------------------------------------------------------------------------- #
# Rollup job                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class RollupResult:
    hours: int = 0
    rows: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    #: Hours older than the refresh window re-rolled because they received rows.
    late_hours: int = 0
    #: Hours rolled before migration 0130 re-rolled to store their per-pod restart increase.
    legacy_hours: int = 0


class WorkloadUsageRollup:
    """Keeps ``workload_usage_hourly`` up to date, one hour at a time.

    Each run re-rolls the last ``refresh_hours`` hours (the carbon-intensity
    recompute still changes them) plus every hour since the last rolled one,
    up to the current (open) hour. It also re-rolls every older hour of the
    backfill window that received raw rows since the previous run, found from
    the raw tables' row ids (input high-water marks saved after each run):
    collection catch-up after an outage (``COLLECTION_MAX_BACKFILL``) or
    ``greenkube start --last`` can write hours far behind the rollup. The
    first run backfills ``backfill_hours``. An hour is rolled from raw rows
    when the raw table still holds it, otherwise from the compressed hourly
    table.
    """

    def __init__(
        self,
        repository: "WorkloadUsageRepository",
        *,
        step_seconds: float = 300.0,
        backfill_hours: int = 14 * 24,
        warmup_seconds: float = 0.0,
    ):
        self.repository = repository
        self.step_seconds = step_seconds
        self.backfill_hours = max(int(backfill_hours), 1)
        self.warmup_seconds = max(float(warmup_seconds or 0.0), 0.0)

    async def _restart_context(self, hour: datetime, pod_rows: Sequence[Any], context: Sequence[Any]) -> List[Any]:
        """Previous restart counts of the hour's pods: the context rows, plus each pod's last
        stored count however old when the context has none for it (collection gap, missing hour)."""
        known = {(row.namespace, row.pod_name) for row in context if _restart_value(row) is not None}
        # Only pods that carry a count need a previous one: a pod without restart counts
        # (kube-state-metrics not scraped) never triggers the unbounded lookup.
        counted = {(row.namespace, row.pod_name) for row in pod_rows if _restart_value(row) is not None}
        missing = sorted(counted - known)
        if not missing:
            return list(context)
        return [*context, *await self.repository.read_last_restart_counts(hour, missing)]

    async def refresh(
        self, now: Optional[datetime] = None, *, refresh_hours: int = DEFAULT_REFRESH_HOURS
    ) -> RollupResult:
        now = now or datetime.now(timezone.utc)
        end = floor_hour(now) + timedelta(hours=1)
        earliest = end - timedelta(hours=self.backfill_hours)
        # Read the input marks before any input row: rows written while this
        # run is rolling are above the saved marks and roll on the next run.
        marks = await self.repository.input_high_water_marks()
        latest = await self.repository.latest_rolled_hour()
        start = end - timedelta(hours=max(int(refresh_hours), 1))
        written: List[datetime] = []
        if latest is None:
            start = earliest
        else:
            start = min(start, floor_hour(latest))
            saved = await self.repository.read_rollup_marks()
            if saved:
                # Hours that received rows since the last run, however old
                # (catch-up after an outage, `start --last`, re-collection).
                written = await self.repository.hours_written_between(saved, marks)
        oldest_input = await self.repository.earliest_input_time()
        if oldest_input is None:
            await self.repository.save_rollup_marks(marks)
            return RollupResult(start=end, end=end)
        floor_input = floor_hour(oldest_input)
        start = max(start, earliest, floor_input)
        hours = {start + timedelta(hours=offset) for offset in range(int((end - start) / timedelta(hours=1)))}
        late = {floor_hour(hour) for hour in written if max(earliest, floor_input) <= floor_hour(hour) < start}
        hours |= late
        # Hours rolled before migration 0130 carry no per-pod restart increase: re-roll
        # those the stored rows can still rebuild (REC-04), once.
        legacy = {
            floor_hour(hour) for hour in await self.repository.legacy_restart_hours(max(earliest, floor_input), start)
        } - hours
        hours |= legacy
        result = RollupResult(start=min(hours) if hours else start, end=end, late_hours=len(late))
        result.legacy_hours = len(legacy)
        if legacy:
            logger.info("Workload usage rollup: re-rolling %d hours rolled before migration 0130.", len(legacy))
        if late:
            logger.info("Workload usage rollup: re-rolling %d hours that received late rows.", len(late))
        for hour in sorted(hours):
            pod_rows, container_rows, source = await self.repository.read_hour_inputs(hour)
            if hour in legacy and not pod_rows:
                # Pod rows (which carry the restart counts) no longer stored: keep the rolled hour.
                continue
            context: List[Any] = []
            if pod_rows:
                # The previous restart count of each pod: per-pod restart increases
                # (REC-04), and a restart shortly before the hour still excludes the
                # hour's first points (warm-up).
                if source == "hourly":
                    context = await self.repository.read_hourly_restart_context(hour)
                else:
                    step = max(self.step_seconds, 1.0)
                    context = await self.repository.read_restart_context(hour, max(self.warmup_seconds, step) + step)
            restart_context = await self._restart_context(hour, pod_rows, context)
            rows = (
                rollup_rows(
                    pod_rows,
                    container_rows,
                    step_seconds=self.step_seconds,
                    source=source,
                    warmup_seconds=self.warmup_seconds,
                    context_rows=context,
                    restart_context_rows=restart_context,
                    collection_started=oldest_input < hour,
                )
                if pod_rows or container_rows
                else []
            )
            if hour in legacy and source != "raw":
                # A pre-0130 hour whose raw rows are gone: the compressed hourly rows would
                # lower the stored percentiles, so only its restart increase is written.
                await self.repository.set_restart_increase(hour, rows)
            else:
                await self.repository.replace_hour(hour, rows)
            result.hours += 1
            result.rows += len(rows)
        await self.repository.save_rollup_marks(marks)
        return result
