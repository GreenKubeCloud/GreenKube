# src/greenkube/collectors/promql.py
"""Single source of truth for every PromQL expression GreenKube sends.

Scoping rules (BUG-01 / roadmap A1):

* **Container-scoped series** (CPU, memory, filesystem I/O and usage) are read
  from cAdvisor, which exports one series per container *and* one series per
  pod cgroup (``container=""``) and, on Docker runtimes, one for the pause
  container (``container="POD"``).  Summing them by pod counts the same CPU
  seconds twice, so every container-scoped selector carries
  ``container!="",container!="POD"``.
* **Duplicate series of one container** (USER BUG 2): the same container can be
  exported several times, with labels the ``by`` clause drops: kubelet
  ``/metrics/resource`` scraped next to ``/metrics/cadvisor``, two scrape jobs,
  or two cgroup paths (``id``) of one container (systemd/cgroupfs driver
  mismatch).  CPU and memory usage are therefore aggregated with ``max`` per
  container instance, never ``sum``: a sum doubled the usage and the
  rightsizing increases built on it.
* **Consecutive instances of one container** (COL-11): a restart, a crash loop
  or a pod recreated under the same name yields one CPU series per instance,
  each with its own runtime container id in cAdvisor's ``name`` label.  Their
  rates cover consecutive parts of the rate window, so CPU is the ``max`` per
  instance (``name``), then the ``sum`` of the instances.  Series without a
  ``name`` label (kubelet ``/metrics/resource``, pod cgroups) keep the ``max``
  per container and are used only when no cAdvisor instance series exists.
  Memory is a gauge and keeps the ``max``.
* **Pod-scoped series** (network) are only exported on the pod sandbox, never
  per application container, so they are intentionally left unfiltered.
* **kube-state-metrics series** (restarts, pod info) describe Kubernetes
  objects, not cgroups, and need no container filter.

``rate_window = max(step, 4 × scrape_interval)`` guarantees at least four
scrapes in every rate window, even with a short step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

CONTAINER_FILTER = 'container!="",container!="POD"'
POD_CGROUP_FILTER = 'container="",pod!=""'
#: Containers and pod cgroups of pods (pause containers and node-level cgroups excluded), COL-09.
POD_LEVEL_OR_CONTAINER_FILTER = 'pod!="",container!="POD"'

DEFAULT_SCRAPE_INTERVAL_SECONDS = 30
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(value: str | int | float, *, field_name: str = "duration") -> int:
    """Parse a Prometheus-style duration (``30s``, ``5m``, ``1h``, ``1d``) into seconds."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a duration such as '30s', '5m' or '1h'.")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        match = _DURATION_RE.match(str(value).strip().lower())
        if not match:
            raise ValueError(f"{field_name} must be a duration such as '30s', '5m' or '1h', got {value!r}.")
        seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2)]
    if seconds <= 0:
        raise ValueError(f"{field_name} must be greater than zero, got {value!r}.")
    return seconds


def _config_duration(config: object, name: str, default: int) -> int:
    value = getattr(config, name, None)
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return default
    try:
        return parse_duration_seconds(value, field_name=name)
    except ValueError:
        return default


def _selector(*matchers: str) -> str:
    parts = [m for m in matchers if m]
    return "{" + ",".join(parts) + "}" if parts else ""


@dataclass(frozen=True)
class PromQLCatalog:
    """Builds every PromQL expression from ``step``, ``rate_window`` and label filters."""

    step_seconds: int
    scrape_interval_seconds: int = DEFAULT_SCRAPE_INTERVAL_SECONDS
    extra_matchers: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if type(self.step_seconds) is not int or self.step_seconds <= 0:
            raise ValueError("step_seconds must be a positive integer.")
        if type(self.scrape_interval_seconds) is not int or self.scrape_interval_seconds <= 0:
            raise ValueError("scrape_interval_seconds must be a positive integer.")

    @classmethod
    def from_config(cls, config: object, *, extra_matchers: Iterable[str] = ()) -> "PromQLCatalog":
        """Build the catalog from settings already validated by :class:`Config`."""
        step = _config_duration(config, "PROMETHEUS_QUERY_RANGE_STEP", 300)
        scrape = _config_duration(config, "PROMETHEUS_SCRAPE_INTERVAL", DEFAULT_SCRAPE_INTERVAL_SECONDS)
        return cls(step_seconds=step, scrape_interval_seconds=scrape, extra_matchers=tuple(extra_matchers))

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    @property
    def step(self) -> str:
        return f"{self.step_seconds}s"

    @property
    def rate_window_seconds(self) -> int:
        return max(self.step_seconds, 4 * self.scrape_interval_seconds)

    @property
    def rate_window(self) -> str:
        return f"{self.rate_window_seconds}s"

    def _container(self, metric: str) -> str:
        return metric + _selector(CONTAINER_FILTER, *self.extra_matchers)

    def _pod(self, metric: str) -> str:
        return metric + _selector(*self.extra_matchers)

    # ------------------------------------------------------------------
    # Container-scoped (cAdvisor) — pod cgroup and pause series excluded
    # ------------------------------------------------------------------

    def _cpu_rate_per_container(self, *matchers: str) -> str:
        """CPU cores per container: instances (``name``) added, duplicates of one instance counted once (COL-11)."""
        named = _selector(*matchers, 'name!=""', *self.extra_matchers)
        unnamed = _selector(*matchers, 'name=""', *self.extra_matchers)
        return (
            f"sum(max(rate(container_cpu_usage_seconds_total{named}[{self.rate_window}])) "
            "by (namespace,pod,container,node,name)) by (namespace,pod,container,node) "
            f"or max(rate(container_cpu_usage_seconds_total{unnamed}[{self.rate_window}])) "
            "by (namespace,pod,container,node)"
        )

    def container_cpu_usage(self) -> str:
        """CPU cores per container: consecutive instances added, duplicate series counted once (COL-11)."""
        return self._cpu_rate_per_container(CONTAINER_FILTER)

    def node_cpu_usage(self) -> str:
        """Whole-node CPU cores of the kubelet root cgroup (``id="/"``): pods, kubelet, runtime, kernel (NRG-12)."""
        selector = _selector('id="/"', *self.extra_matchers)
        return f"max(rate(container_cpu_usage_seconds_total{selector}[{self.rate_window}])) by (node)"

    def pod_cgroup_cpu_usage(self) -> str:
        """Fallback when no container series exist: the pod cgroup series only, counted once."""
        selector = _selector(POD_CGROUP_FILTER, *self.extra_matchers)
        return f"max(rate(container_cpu_usage_seconds_total{selector}[{self.rate_window}])) by (namespace,pod,node)"

    def cpu_usage_by_level(self) -> str:
        """CPU cores of every container and of every pod cgroup, in one query (COL-09).

        ``container=""`` series are pod cgroups, the others containers.  The
        caller chooses per pod: its containers when it has any, else its pod
        cgroup (counted once), so a cluster mixing runtimes (cri-dockerd
        exporting pod-level series next to containerd) keeps every pod.
        """
        return self._cpu_rate_per_container(POD_LEVEL_OR_CONTAINER_FILTER)

    def memory_working_set_by_level(self) -> str:
        """Average working-set bytes over the step of every container and pod cgroup (COL-09, COL-12).

        The step average, not the instant value at the step boundary, is the
        memory a row holds for allocation; the caller chooses per pod between
        its containers and its pod cgroup.
        """
        selector = _selector(POD_LEVEL_OR_CONTAINER_FILTER, *self.extra_matchers)
        return (
            f"max(avg_over_time(container_memory_working_set_bytes{selector}[{self.step}])) "
            "by (namespace,pod,container,node)"
        )

    def memory_working_set_peak_by_level(self) -> str:
        """Largest working-set scrape inside the step of every container and pod cgroup (COL-12).

        Stored as ``memory_usage_max_bytes`` on container rows: rightsizing and
        risk checks size memory on the peaks between two evaluation points.
        """
        selector = _selector(POD_LEVEL_OR_CONTAINER_FILTER, *self.extra_matchers)
        return (
            f"max(max_over_time(container_memory_working_set_bytes{selector}[{self.step}])) "
            "by (namespace,pod,container,node)"
        )

    def container_cpu_counter_max(self) -> str:
        """Largest CPU counter value of each container within the step (COL-03 step 3).

        ``rate()`` needs two scrapes of one series, so a container that lived
        less than about two scrape intervals has no rate sample.  Its counter
        starts at 0 with the container: when the series first appears inside a
        step, its value over the step length is the container's core usage.
        """
        return (
            f"max(max_over_time({self._container('container_cpu_usage_seconds_total')}[{self.step}])) "
            "by (namespace,pod,container,node)"
        )

    def container_start_time(self) -> str:
        """cAdvisor start time (Unix seconds) of each container: proves a counter really started (review 1)."""
        return f"max({self._container('container_start_time_seconds')}) by (namespace,pod,container,node)"

    def container_memory_working_set(self) -> str:
        """Working-set bytes per container (duplicate series counted once)."""
        return f"max({self._container('container_memory_working_set_bytes')}) by (namespace,pod,container,node)"

    def pod_cgroup_memory_working_set(self) -> str:
        """Fallback when no container series exist: the pod cgroup working set, counted once.

        Some kubelets (e.g. Minikube with the Docker driver) only export pod-level
        cgroups, without a ``container`` label. The value is the whole pod's
        working set: it is never attributed to a container.
        """
        selector = _selector(POD_CGROUP_FILTER, *self.extra_matchers)
        return f"max(container_memory_working_set_bytes{selector}) by (namespace,pod,node)"

    def pod_disk_read(self) -> str:
        """Filesystem read rate summed over the pod's containers."""
        return (
            f"sum(rate({self._container('container_fs_reads_bytes_total')}[{self.rate_window}])) "
            "by (namespace,pod,node)"
        )

    def pod_disk_write(self) -> str:
        """Filesystem write rate summed over the pod's containers."""
        return (
            f"sum(rate({self._container('container_fs_writes_bytes_total')}[{self.rate_window}])) "
            "by (namespace,pod,node)"
        )

    def pod_ephemeral_storage_usage(self) -> str:
        """Writable-layer and log usage summed over the pod's containers."""
        return f"sum({self._container('container_fs_usage_bytes')}) by (namespace,pod,node)"

    # ------------------------------------------------------------------
    # Pod-scoped (sandbox network namespace) — intentionally unfiltered
    # ------------------------------------------------------------------

    def pod_network_receive(self) -> str:
        return (
            f"sum(rate({self._pod('container_network_receive_bytes_total')}[{self.rate_window}])) "
            "by (namespace,pod,node)"
        )

    def pod_network_transmit(self) -> str:
        return (
            f"sum(rate({self._pod('container_network_transmit_bytes_total')}[{self.rate_window}])) "
            "by (namespace,pod,node)"
        )

    # ------------------------------------------------------------------
    # kube-state-metrics
    # ------------------------------------------------------------------

    def pod_restarts(self) -> str:
        return f"sum({self._pod('kube_pod_container_status_restarts_total')}) by (namespace,pod)"

    def pod_node(self) -> str:
        """Pod → node placement, used when cAdvisor series lack a ``node`` label."""
        selector = _selector('node!=""', *self.extra_matchers)
        return f"max(kube_pod_info{selector}) by (namespace,pod,node)"

    def _ksm_selector(self, pods: Sequence[str], *matchers: str) -> str:
        """Selector of kube-state-metrics series restricted to ``pods`` (no filter when empty)."""
        names = "|".join(sorted({pod.replace(".", "[.]") for pod in pods}))
        return _selector(*matchers, f'pod=~"{names}"' if names else "", *self.extra_matchers)

    def pod_container_requests(self, pods: Sequence[str] = (), *, init: bool = False) -> str:
        """kube-state-metrics CPU (cores) and memory (bytes) requests per container (WP-01.1 step 2).

        ``init`` reads init containers (native sidecars are kept by the caller).
        """
        metric = "kube_pod_init_container_resource_requests" if init else "kube_pod_container_resource_requests"
        selector = self._ksm_selector(pods, 'resource=~"cpu|memory"')
        return f"max({metric}{selector}) by (namespace,pod,container,resource)"

    def pod_container_limits(self, pods: Sequence[str] = (), *, init: bool = False) -> str:
        """kube-state-metrics CPU (cores) and memory (bytes) limits per container (WP-01.1 step 2)."""
        metric = "kube_pod_init_container_resource_limits" if init else "kube_pod_container_resource_limits"
        selector = self._ksm_selector(pods, 'resource=~"cpu|memory"')
        return f"max({metric}{selector}) by (namespace,pod,container,resource)"

    def pod_container_info(self, pods: Sequence[str] = ()) -> str:
        """Every container of each pod, with or without requests (kube-state-metrics)."""
        return f"max(kube_pod_container_info{self._ksm_selector(pods)}) by (namespace,pod,container)"

    def pod_sidecar_info(self, pods: Sequence[str] = ()) -> str:
        """Native sidecars: init containers with ``restartPolicy: Always`` (kube-state-metrics 2.10+)."""
        selector = self._ksm_selector(pods, 'restart_policy="Always"')
        return f"max(kube_pod_init_container_info{selector}) by (namespace,pod,container)"

    def pod_owner(self, pods: Sequence[str] = ()) -> str:
        """kube-state-metrics controller owner of each pod (WP-01.1 step 2)."""
        selector = self._ksm_selector(pods, 'owner_is_controller="true"')
        return f"max(kube_pod_owner{selector}) by (namespace,pod,owner_kind,owner_name)"

    def scrape_probe(self) -> str:
        """Number of cAdvisor CPU series, pod or not (COL-04).

        The kubelet exports the root and system cgroups even on a node without
        pods, so a step where this is absent had no cAdvisor scrape at all:
        Prometheus has no data, which is a hole, not an empty cluster.
        """
        return f"count({self._pod('container_cpu_usage_seconds_total')})"

    def rate_window_scrapes(self) -> str:
        """Most scrapes any cAdvisor CPU series holds inside the rate window (COL-04 step 3).

        ``rate()`` needs at least two samples of a series in its window: fewer
        means ``PROMETHEUS_SCRAPE_INTERVAL`` is set shorter than the real
        scrape interval and every window comes back empty.
        """
        return f"max(count_over_time({self._pod('container_cpu_usage_seconds_total')}[{self.rate_window}]))"

    def pod_running(self) -> str:
        """Pods in phase Running (value 1), so a running pod without a CPU sample still gets its row (COL-03)."""
        selector = _selector('phase="Running"', *self.extra_matchers)
        return f"max(kube_pod_status_phase{selector}) by (namespace,pod)"
