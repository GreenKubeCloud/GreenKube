# src/greenkube/models/workload_usage.py
"""Workload usage rollups and per-target summaries (roadmap E3, BUG-12).

Recommendation scans no longer materialize every raw metric row. The
``workload_usage_hourly`` rollup keeps one row per hour, workload target,
container and node; scans read one :class:`WorkloadUsageSummary` per target and
container, computed in SQL over the lookback window.

Identity: a target is ``(namespace, target_kind, target_name)``, resolved like
:func:`greenkube.core.optimization.targeting.target_key` (owner, inferred
Deployment, or the pod itself with kind ``Pod``). ``container_name`` is ``""``
for the pod-level aggregate, which exists for every pod; container rows exist
only where container metrics were collected.

Usage percentiles are taken over per-step values: the per-step maximum when the
collector stored one, otherwise the per-step value (a step average for CPU, a
sample for memory). Hourly percentiles are exact over the hour's steps;
lookback percentiles are the nearest-rank percentile of the hourly percentiles
(docs/recommendation.md, "Usage summaries").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from pydantic import Field

from greenkube.models.metrics import CombinedMetric

#: Container name of the pod-level aggregate rows.
POD_LEVEL = ""

#: Percentiles stored per hour and summarized over the lookback (nearest rank).
USAGE_PERCENTILES: Tuple[int, ...] = (50, 90, 95, 99)

TargetKey = Tuple[str, str, str]


@dataclass(slots=True)
class WorkloadUsageHour:
    """One ``workload_usage_hourly`` row: a target's container on one node for one hour."""

    hour_bucket: Optional[datetime]
    namespace: str
    target_kind: str
    target_name: str
    container_name: str = POD_LEVEL
    node: str = ""
    hour_of_day: Optional[int] = None
    #: Weighted samples (a raw row weighs 1, an hourly row its ``sample_count``).
    sample_count: int = 0
    #: Distinct pods observed in the hour.
    pod_count: int = 0
    #: Collection steps observed in the hour; ``sample_count / step_count`` is the average replica count.
    step_count: int = 0
    #: Replica-seconds observed.
    duration_seconds: float = 0.0
    cpu_sample_count: int = 0
    cpu_usage_avg: Optional[float] = None
    #: Mean of squared per-step values (variance of usage over steps).
    cpu_usage_sq_avg: Optional[float] = None
    cpu_usage_p50: Optional[float] = None
    cpu_usage_p90: Optional[float] = None
    cpu_usage_p95: Optional[float] = None
    cpu_usage_p99: Optional[float] = None
    cpu_usage_max: Optional[float] = None
    memory_sample_count: int = 0
    memory_usage_avg: Optional[float] = None
    memory_usage_p50: Optional[float] = None
    memory_usage_p90: Optional[float] = None
    memory_usage_p95: Optional[float] = None
    memory_usage_p99: Optional[float] = None
    memory_usage_max: Optional[float] = None
    #: Latest request and limit observed in the hour (per replica).
    cpu_request: int = 0
    memory_request: int = 0
    #: Sum of per-sample requests (request x weight): exact requested capacity over the hour.
    cpu_request_sum: float = 0.0
    memory_request_sum: float = 0.0
    cpu_limit: Optional[int] = None
    memory_limit: Optional[int] = None
    restart_count: Optional[int] = None
    total_cost: float = 0.0
    cost_cpu: Optional[float] = None
    cost_ram: Optional[float] = None
    cost_storage: Optional[float] = None
    cost_network: Optional[float] = None
    cost_lb: Optional[float] = None
    cost_source: Optional[str] = None
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    joules: float = 0.0
    #: DCGM GPU energy attributed to the pods (``gpu_power_watts`` x seconds, B5); None without GPU data.
    gpu_watt_seconds: Optional[float] = None
    pue: Optional[float] = None
    grid_intensity: Optional[float] = None
    #: Samples that carried a positive grid intensity (weight of ``grid_intensity``).
    intensity_sample_count: int = 0
    emaps_zone: Optional[str] = None
    node_instance_type: Optional[str] = None
    #: ``raw`` when rolled up from raw rows, ``hourly`` from the compressed hourly tables.
    source: str = "raw"
    #: Seconds from ``hour_bucket`` to the row's latest sample: among the node rows of
    #: one hour, the newest one holds the current request (None for rows before 0076).
    latest_sample_offset_seconds: Optional[float] = None

    @property
    def target(self) -> TargetKey:
        return (self.namespace, self.target_kind, self.target_name)


@dataclass(frozen=True, slots=True)
class UsageStats:
    """Distribution of one resource's per-replica usage over the lookback.

    Every value is per container and per replica: one point per replica and
    collection step, never a sum across replicas or pods. Percentiles exclude
    the warm-up after a container (re)start; ``max`` is every observed value.
    """

    sample_count: int
    avg: float
    p50: float
    p90: float
    p95: float
    p99: float
    max: float
    #: Standard deviation of per-step values; None when unknown.
    stddev: Optional[float] = None

    def percentile(self, p: int) -> float:
        return {50: self.p50, 90: self.p90, 95: self.p95, 99: self.p99, 100: self.max}[p]


@dataclass(slots=True)
class WorkloadUsageSummary:
    """Usage of one target container (or the pod-level aggregate) over the lookback (~1 KB)."""

    namespace: str
    target_kind: str
    target_name: str
    container_name: str = POD_LEVEL
    first_hour: Optional[datetime] = None
    last_hour: Optional[datetime] = None
    hours_observed: int = 0
    sample_count: int = 0
    step_count: int = 0
    duration_seconds: float = 0.0
    replicas_max: int = 0
    cpu: Optional[UsageStats] = None
    memory: Optional[UsageStats] = None
    cpu_request: int = 0
    memory_request: int = 0
    cpu_limit: Optional[int] = None
    memory_limit: Optional[int] = None
    restart_count: Optional[int] = None
    total_cost: float = 0.0
    co2e_grams: float = 0.0
    embodied_co2e_grams: float = 0.0
    joules: float = 0.0
    grid_intensity: Optional[float] = None
    intensity_sample_count: int = 0
    emaps_zone: Optional[str] = None
    #: Reliability signals over the lookback, when a source provided them (None = unknown).
    #: Filled by scans from kube-state-metrics / cAdvisor (``greenkube.core.reliability_signals``).
    oom_kill_count: Optional[int] = None
    #: Container restarts over the lookback (kube-state-metrics), 0 when it reports none, None when unknown.
    restarts_in_window: Optional[int] = None
    throttling_ratio: Optional[float] = None
    #: Why the container is believed to run a JVM (``JAVA_TOOL_OPTIONS``, ``-Xmx``, image name), None when not.
    jvm_hint: Optional[str] = None
    #: Pod-spec list of the container (H1): ``container``, ``sidecar`` or ``init``; None when not read.
    container_kind: Optional[str] = None

    @property
    def target(self) -> TargetKey:
        return (self.namespace, self.target_kind, self.target_name)

    @property
    def container(self) -> Optional[str]:
        """Container name, or None for the pod-level aggregate."""
        return self.container_name or None

    @property
    def coverage_seconds(self) -> Optional[float]:
        """Span from the first to the end of the last observed hour, or None when unknown."""
        if self.first_hour is None or self.last_hour is None:
            return None
        return (self.last_hour - self.first_hour).total_seconds() + 3600.0

    @property
    def replicas_avg(self) -> Optional[float]:
        if self.step_count <= 0:
            return None
        return self.sample_count / self.step_count


@dataclass(slots=True)
class NodeUsageSummary:
    """Average concurrent usage and requests of the pods on one node over the lookback."""

    node: str
    hours_observed: int = 0
    cpu_usage_avg: float = 0.0
    memory_usage_avg: float = 0.0
    cpu_request_avg: float = 0.0
    memory_request_avg: float = 0.0


@dataclass
class UsageSnapshot:
    """Everything usage-based analyzers read, bounded by the number of targets."""

    #: ``{target: {container_name: summary}}`` with ``""`` for the pod-level aggregate.
    summaries: Dict[TargetKey, Dict[str, WorkloadUsageSummary]] = field(default_factory=dict)
    #: Pod-level average CPU per UTC hour of day (24 entries, None when unobserved).
    hour_profiles: Dict[TargetKey, Tuple[Optional[float], ...]] = field(default_factory=dict)
    #: Samples behind each hour-of-day profile.
    hour_profile_samples: Dict[TargetKey, int] = field(default_factory=dict)
    nodes: Dict[str, NodeUsageSummary] = field(default_factory=dict)
    #: ``rollup`` (SQL over ``workload_usage_hourly``) or ``rows`` (computed from metric rows).
    source: str = "rows"
    #: First observed hour across all targets (None when unknown).
    first_hour: Optional[datetime] = None


class BaselineMetric(CombinedMetric):
    """A ``CombinedMetric`` aggregated from the rollup for projections and evidence.

    One row stands for every sample of a target container on one node (and,
    for pod-level rows, one UTC hour of day) with the same requests; its
    ``timestamp`` is the last hour it covers, so the newest rows carry the
    current requests. The usage percentiles of the target's summary travel
    with the row so evidence statistics match the analyzer's.
    """

    cpu_usage_p50: Optional[float] = Field(None, exclude=True)
    cpu_usage_p90: Optional[float] = Field(None, exclude=True)
    cpu_usage_p95: Optional[float] = Field(None, exclude=True)
    cpu_usage_p99: Optional[float] = Field(None, exclude=True)
    memory_usage_p50: Optional[float] = Field(None, exclude=True)
    memory_usage_p90: Optional[float] = Field(None, exclude=True)
    memory_usage_p95: Optional[float] = Field(None, exclude=True)
    memory_usage_p99: Optional[float] = Field(None, exclude=True)
