# src/greenkube/collectors/promql.py
"""Single source of truth for every PromQL expression GreenKube sends.

Scoping rules (BUG-01 / roadmap A1):

* **Container-scoped series** (CPU, memory, filesystem I/O and usage) are read
  from cAdvisor, which exports one series per container *and* one series per
  pod cgroup (``container=""``) and, on Docker runtimes, one for the pause
  container (``container="POD"``).  Summing them by pod counts the same CPU
  seconds twice, so every container-scoped selector carries
  ``container!="",container!="POD"``.
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
from typing import Iterable

CONTAINER_FILTER = 'container!="",container!="POD"'
POD_CGROUP_FILTER = 'container="",pod!=""'

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

    def container_cpu_usage(self) -> str:
        """CPU cores per container: ``sum by (namespace,pod,container,node)``."""
        return (
            f"sum(rate({self._container('container_cpu_usage_seconds_total')}[{self.rate_window}])) "
            "by (namespace,pod,container,node)"
        )

    def pod_cgroup_cpu_usage(self) -> str:
        """Fallback when no container series exist: the pod cgroup series only, counted once."""
        selector = _selector(POD_CGROUP_FILTER, *self.extra_matchers)
        return f"sum(rate(container_cpu_usage_seconds_total{selector}[{self.rate_window}])) by (namespace,pod,node)"

    def container_memory_working_set(self) -> str:
        """Working-set bytes per container."""
        return f"sum({self._container('container_memory_working_set_bytes')}) by (namespace,pod,container,node)"

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

    def container_restarts(self) -> str:
        return f"sum({self._pod('kube_pod_container_status_restarts_total')}) by (namespace,pod,container)"

    def pod_node(self) -> str:
        """Pod → node placement, used when cAdvisor series lack a ``node`` label."""
        selector = _selector('node!=""', *self.extra_matchers)
        return f"max(kube_pod_info{selector}) by (namespace,pod,node)"

    @staticmethod
    def node_instance_labels(label_key: str) -> str:
        return f"kube_node_labels{{{label_key}!=''}}"
