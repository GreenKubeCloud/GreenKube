# src/greenkube/core/optimization/context.py
"""Execution context passed to recommendation sources and analyzers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Set, Tuple

from greenkube.models.metrics import CombinedMetric
from greenkube.models.workload_usage import POD_LEVEL, TargetKey, UsageSnapshot, WorkloadUsageSummary

if TYPE_CHECKING:
    from greenkube.core.config import Config


@dataclass
class OptimizationContext:
    """All inputs required to generate recommendations for one analysis run.

    The context is built once per scan by
    :class:`~greenkube.core.optimization.context_builder.ContextBuilder` and
    shared by every recommendation source.

    Analyzers read :meth:`usage_snapshot`: one usage summary per target
    container, computed in SQL from the ``workload_usage_hourly`` rollup
    (roadmap E3). ``metrics`` and ``container_metrics`` then hold baseline
    rows aggregated from the same rollup, which the Impact Model and the
    evidence builder consume. A context built from in-memory metric rows (CLI
    ``--live``, tests) derives the same snapshot from its rows.
    """

    config: "Config"
    metrics: List[CombinedMetric] = field(default_factory=list)
    container_metrics: List[CombinedMetric] = field(default_factory=list)
    namespace: Optional[str] = None
    analysis_window_seconds: Optional[float] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    node_infos: List = field(default_factory=list)
    hpa_targets: Optional[Set[Tuple[str, str, str]]] = None
    persistent_volumes: Optional[List] = None
    load_balancers: Optional[List] = None
    #: Namespace metadata by name for the deletion safety rules; None when unreadable.
    namespace_info: Optional[Dict[str, Any]] = None
    usage: Optional[UsageSnapshot] = None
    #: Rightsizing decreases withheld by this scan because the restart/OOM history
    #: could not be read (REC-04), as ``(namespace, target_kind, target_name,
    #: container, resource)``; filled by the analyzers, read by the engine.
    withheld_decreases: Set[Tuple[str, str, str, str, str]] = field(default_factory=set)
    _target_series_cache: Optional[Dict[Tuple[str, str, str], List[CombinedMetric]]] = field(
        default=None, init=False, repr=False, compare=False
    )
    _pod_series_cache: Optional[Dict[Tuple[str, str, str], List[CombinedMetric]]] = field(
        default=None, init=False, repr=False, compare=False
    )

    def target_series(self) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
        """Returns metrics grouped by stable workload owner, computed once.

        Container rows replace the pod rows of the pods they belong to.
        """
        if self._target_series_cache is None:
            from greenkube.core.optimization.targeting import group_by_recommendation_target

            containerized_pods = {(metric.namespace, metric.pod_name) for metric in self.container_metrics}
            target_metrics = [
                metric for metric in self.metrics if (metric.namespace, metric.pod_name) not in containerized_pods
            ]
            target_metrics.extend(self.container_metrics)
            self._target_series_cache = group_by_recommendation_target(target_metrics)
        return self._target_series_cache

    def pod_series(self) -> Dict[Tuple[str, str, str], List[CombinedMetric]]:
        """Returns the pod-level rows (never container rows) grouped by target, computed once."""
        if self._pod_series_cache is None:
            from greenkube.core.optimization.targeting import group_by_recommendation_target

            self._pod_series_cache = group_by_recommendation_target(
                [metric for metric in self.metrics if not metric.container_name]
            )
        return self._pod_series_cache

    def workload_rows(self, target: TargetKey) -> List[CombinedMetric]:
        """Rows that price a whole-workload change (deletion, schedule): pod level when available."""
        return self.pod_series().get(target) or self.target_series().get(target, [])

    def container_rows(self, target: TargetKey, container: Optional[str]) -> List[CombinedMetric]:
        """Rows that price a request change of one container (``None`` = pod-level aggregate)."""
        series = self.target_series().get(target, [])
        if container is None:
            pod_rows = [metric for metric in series if not metric.container_name]
            return pod_rows or series
        return [metric for metric in series if metric.container_name == container]

    # ------------------------------------------------------------------ #
    # Usage summaries (roadmap E3)                                        #
    # ------------------------------------------------------------------ #

    def usage_snapshot(self) -> UsageSnapshot:
        """Usage summaries per target container, computed once from the rows when not preloaded."""
        if self.usage is None:
            from greenkube.core.workload_usage import summarize_rows
            from greenkube.utils.time import parse_query_step_seconds

            step = parse_query_step_seconds(getattr(self.config, "PROMETHEUS_QUERY_RANGE_STEP", "5m") or "5m")
            pod_rows = [metric for metric in self.metrics if not metric.container_name]
            container_rows = [metric for metric in self.metrics if metric.container_name]
            container_rows.extend(self.container_metrics)
            warmup = int(getattr(self.config, "RIGHTSIZING_WARMUP_MINUTES", 0) or 0) * 60
            self.usage = summarize_rows(pod_rows, container_rows, step_seconds=step, warmup_seconds=warmup)
        return self.usage

    def workload_summaries(self) -> Iterator[Tuple[TargetKey, WorkloadUsageSummary]]:
        """Pod-level usage summary of every target."""
        for target, containers in self.usage_snapshot().summaries.items():
            summary = containers.get(POD_LEVEL)
            if summary is not None:
                yield target, summary

    def container_summaries(self, target: TargetKey) -> List[Tuple[Optional[str], WorkloadUsageSummary]]:
        """Usage summaries of a target's containers, or its pod-level summary when it has none.

        Mirrors :func:`~greenkube.core.optimization.targeting.split_series_by_container`:
        once container identities exist, the pod-level aggregate is never mixed in.
        """
        containers = self.usage_snapshot().summaries.get(target, {})
        named = [(name, summary) for name, summary in sorted(containers.items()) if name != POD_LEVEL]
        if named:
            return [(name, summary) for name, summary in named]
        summary = containers.get(POD_LEVEL)
        return [(None, summary)] if summary is not None else []

    def iter_container_summaries(self) -> Iterator[Tuple[TargetKey, Optional[str], WorkloadUsageSummary]]:
        for target in self.usage_snapshot().summaries:
            for container, summary in self.container_summaries(target):
                yield target, container, summary

    def namespace_targets(self) -> Dict[str, List[WorkloadUsageSummary]]:
        """Pod-level summaries grouped by namespace."""
        grouped: Dict[str, List[WorkloadUsageSummary]] = defaultdict(list)
        for target, summary in self.workload_summaries():
            grouped[target[0]].append(summary)
        return grouped
