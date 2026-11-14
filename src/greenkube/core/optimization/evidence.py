# src/greenkube/core/optimization/evidence.py
"""Builds review-grade evidence blocks for generated recommendations."""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Sequence

from greenkube.core.optimization.statistics import latest_request_value, percentile, usage_stats
from greenkube.models.evidence import (
    LimitChange,
    ProposedChange,
    RecommendationEvidence,
    ResourceSnapshot,
    RollbackCondition,
    UtilizationStats,
)
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType
from greenkube.models.workload_usage import BaselineMetric
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)

_STEP_UNITS = {"s": 1, "m": 60, "h": 3600}


def _step_seconds(config: "Config") -> int:
    """Returns the Prometheus query step in seconds, defaulting to 5 minutes."""
    raw = (getattr(config, "PROMETHEUS_QUERY_RANGE_STEP", "") or "5m").strip().lower()
    try:
        return int(raw[:-1]) * _STEP_UNITS[raw[-1]]
    except (ValueError, KeyError, IndexError):
        return 300


def _humanize_bytes(value: int) -> str:
    """Returns a compact Kubernetes memory quantity string."""
    for suffix, factor in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if value >= factor and value % factor == 0:
            return f"{value // factor}{suffix}"
    return str(value)


def series_for(rec: Recommendation, context: "OptimizationContext") -> List[CombinedMetric]:
    """Returns the metric series backing a recommendation's target."""
    if rec.target_node:
        series = [m for m in context.metrics if m.node == rec.target_node]
    elif rec.owner_kind and rec.owner_name:
        key = (rec.namespace or "", rec.owner_kind, rec.owner_name)
        series = context.target_series().get(key, [])
    elif rec.pod_name:
        series = []
        for key, series in context.target_series().items():
            if key[0] == rec.namespace and key[2] == rec.pod_name:
                break
        else:
            series = [m for m in context.metrics if m.namespace == rec.namespace and m.pod_name == rec.pod_name]
    elif rec.namespace:
        series = [m for m in context.metrics if m.namespace == rec.namespace]
    else:
        series = []

    if rec.container_name is None:
        return series
    if not rec.container_name or rec.container_name.strip() != rec.container_name:
        return []
    return [metric for metric in series if metric.container_name == rec.container_name]


def _utilization_stats(
    series: Sequence[CombinedMetric],
    usage_attr: str,
    max_attr: str,
    window_seconds: Optional[float],
    step_seconds: int,
) -> Optional[UtilizationStats]:
    avg, observed_max, points = usage_stats(list(series), usage_attr, max_attr)
    if not points:
        return None

    sample_count = sum(
        max(int(getattr(m, "sample_count", 1) or 1), 1) for m in series if getattr(m, usage_attr) is not None
    )
    coverage = 1.0
    if window_seconds and window_seconds > 0 and step_seconds > 0:
        expected = max(int(window_seconds // step_seconds), 1)
        coverage = min(1.0, sample_count / expected)

    return UtilizationStats(
        avg=avg,
        p50=percentile(points, 50),
        p90=percentile(points, 90),
        p95=percentile(points, 95),
        p99=percentile(points, 99),
        max=observed_max,
        sample_count=sample_count,
        coverage_ratio=coverage,
    )


def _changes(rec: Recommendation, current: ResourceSnapshot, proposed: ResourceSnapshot) -> List[ProposedChange]:
    changes: List[ProposedChange] = []

    def _ratio(old: Optional[int], new: Optional[int]) -> Optional[float]:
        if old is None or new is None or old <= 0:
            return None
        return (old - new) / old

    if rec.recommended_cpu_request_millicores is not None:
        changes.append(
            ProposedChange(
                resource="cpu",
                current=current.cpu_request_millicores,
                proposed=proposed.cpu_request_millicores,
                change_ratio=_ratio(current.cpu_request_millicores, proposed.cpu_request_millicores),
            )
        )
    if rec.recommended_memory_request_bytes is not None:
        changes.append(
            ProposedChange(
                resource="memory",
                current=current.memory_request_bytes,
                proposed=proposed.memory_request_bytes,
                change_ratio=_ratio(current.memory_request_bytes, proposed.memory_request_bytes),
            )
        )
    return changes


def _rollback_conditions(
    rec: Recommendation,
    cpu_usage: Optional[UtilizationStats],
    memory_usage: Optional[UtilizationStats],
    proposed: ResourceSnapshot,
) -> List[RollbackCondition]:
    conditions: List[RollbackCondition] = []

    if rec.type == RecommendationType.RIGHTSIZING_CPU and proposed.cpu_request_millicores:
        threshold = proposed.cpu_request_millicores * 1.1
        conditions.append(
            RollbackCondition(
                metric="cpu_p95_usage",
                comparator="gt",
                threshold=threshold,
                description="p95 CPU usage exceeds 110% of the proposed request",
            )
        )
        conditions.append(
            RollbackCondition(
                metric="restart_rate",
                comparator="gt",
                threshold=0.0,
                description="any new restart after apply",
            )
        )
    elif rec.type == RecommendationType.RIGHTSIZING_MEMORY and proposed.memory_request_bytes:
        threshold = proposed.memory_request_bytes * 1.1
        conditions.append(
            RollbackCondition(
                metric="memory_p95_usage",
                comparator="gt",
                threshold=threshold,
                description="p95 memory usage exceeds 110% of the proposed request",
            )
        )
        conditions.append(
            RollbackCondition(
                metric="oom_events",
                comparator="gt",
                threshold=0.0,
                description="any OOM kill after apply",
            )
        )
    elif rec.type in (RecommendationType.OFF_PEAK_SCALING, RecommendationType.AUTOSCALING_CANDIDATE):
        conditions.append(
            RollbackCondition(
                metric="availability_ratio",
                comparator="lt",
                threshold=0.99,
                description="readiness drops below 99% during the scale window",
            )
        )
    elif rec.type in (RecommendationType.OVERPROVISIONED_NODE, RecommendationType.UNDERUTILIZED_NODE):
        conditions.append(
            RollbackCondition(
                metric="node_cpu_utilization",
                comparator="gt",
                threshold=0.8,
                action="review",
                description="node utilization exceeds 80% after consolidation",
            )
        )
    return conditions


def _savings_method(rec: Recommendation) -> str:
    impact = rec.impact or {}
    if impact.get("model_version") and impact.get("kind"):
        # Savings projected by the Impact Model (docs/methodology/impact-model.md).
        return f"impact_model/{impact['model_version']}/{impact['kind']}"
    if rec.type in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY):
        return "request_reduction_ratio"
    if rec.type in (RecommendationType.ZOMBIE_POD, RecommendationType.IDLE_NAMESPACE):
        return "observed_cost_annualized"
    if rec.type in (RecommendationType.ORPHANED_PERSISTENT_VOLUME, RecommendationType.ORPHANED_LOAD_BALANCER):
        return "opencost_or_flat_estimate"
    if rec.type == RecommendationType.CARBON_AWARE_SCHEDULING:
        return "high_carbon_share"
    return "unknown"


def build_patch(rec: Recommendation, action=None) -> Optional[dict]:
    """Builds the legacy patch view, requiring an explicit namespace and container."""
    if rec.type not in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY):
        return None
    if (
        not rec.owner_kind
        or rec.owner_kind.strip() != rec.owner_kind
        or not rec.owner_name
        or rec.owner_name.strip() != rec.owner_name
        or not rec.namespace
        or rec.namespace.strip() != rec.namespace
        or not rec.container_name
        or rec.container_name.strip() != rec.container_name
    ):
        return None

    operations: List[dict] = []
    if rec.recommended_cpu_request_millicores is not None:
        operations.append(
            {
                "op": "set_container_resources",
                "resource": "cpu",
                "field": "requests",
                "value": f"{rec.recommended_cpu_request_millicores}m",
            }
        )
    if rec.recommended_memory_request_bytes is not None:
        operations.append(
            {
                "op": "set_container_resources",
                "resource": "memory",
                "field": "requests",
                "value": _humanize_bytes(rec.recommended_memory_request_bytes),
            }
        )
    if not operations:
        return None

    patch = {
        "kind": rec.owner_kind,
        "namespace": rec.namespace,
        "name": rec.owner_name,
        "container_name": rec.container_name,
        "operations": operations,
    }
    if action is not None:
        patch["action"] = action.model_dump(mode="json")
    return patch


def usage_summary_for(rec: Recommendation, context: "OptimizationContext") -> Optional[Any]:
    """The usage summary of the recommendation's target container, with its reliability signals (E9)."""
    if not rec.namespace or rec.target_node or context.usage is None:
        return None
    if rec.owner_kind and rec.owner_name:
        target = (rec.namespace, rec.owner_kind, rec.owner_name)
    elif rec.pod_name:
        target = (rec.namespace, "Pod", rec.pod_name)
    else:
        return None
    return context.usage.summaries.get(target, {}).get(rec.container_name or "")


def build_evidence(rec: Recommendation, context: "OptimizationContext") -> Optional[RecommendationEvidence]:
    """Builds the evidence block for a recommendation, or None when no metrics exist."""
    series = series_for(rec, context)
    if not series:
        return None

    cfg = context.config
    window = context.analysis_window_seconds
    step = _step_seconds(cfg)

    cpu_usage = _utilization_stats(series, "cpu_usage_millicores", "cpu_usage_max_millicores", window, step)
    memory_usage = _utilization_stats(series, "memory_usage_bytes", "memory_usage_max_bytes", window, step)

    current = ResourceSnapshot(
        cpu_request_millicores=latest_request_value(series, "cpu_request") or None,
        memory_request_bytes=latest_request_value(series, "memory_request") or None,
    )
    proposed = ResourceSnapshot(
        cpu_request_millicores=rec.recommended_cpu_request_millicores,
        memory_request_bytes=rec.recommended_memory_request_bytes,
    )

    restart_count = max((m.restart_count or 0 for m in series), default=0)
    total_cost = sum(m.total_cost for m in series)
    total_co2e = sum(m.co2e_grams for m in series)
    cost_per_hour = max(total_cost / window * 3600, 0.0) if window else None
    co2e_per_hour = (total_co2e / window * 3600) if window else None

    # OOM kills and CPU throttling over the lookback, from the scan's usage summary
    # (kube-state-metrics / cAdvisor); None when no source reported them.
    summary = usage_summary_for(rec, context)
    oom_events = getattr(summary, "oom_kill_count", None)
    throttling = getattr(summary, "throttling_ratio", None)
    # Container limits and the limit changes delivered with the request change (H1).
    current = current.model_copy(
        update={
            "cpu_limit_millicores": _positive_int(getattr(summary, "cpu_limit", None)),
            "memory_limit_bytes": _positive_int(getattr(summary, "memory_limit", None)),
        }
    )
    limit_changes = _limit_changes(rec, context, summary)
    for change in limit_changes:
        field_name = "cpu_limit_millicores" if change.resource == "cpu" else "memory_limit_bytes"
        proposed = proposed.model_copy(update={field_name: change.proposed})

    sample_count = max((cpu_usage.sample_count if cpu_usage else 0), (memory_usage.sample_count if memory_usage else 0))
    coverage = max(
        cpu_usage.coverage_ratio if cpu_usage else 0.0,
        memory_usage.coverage_ratio if memory_usage else 0.0,
    )

    return RecommendationEvidence(
        observation_window_start=context.window_start,
        observation_window_end=context.window_end,
        observation_window_seconds=window,
        first_observed_at=first_observed_at(series, summary, context, step),
        sample_count=sample_count,
        coverage_ratio=coverage or 1.0,
        current=current,
        proposed=proposed,
        changes=_changes(rec, current, proposed),
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        restart_count=restart_count,
        oom_events=oom_events if isinstance(oom_events, int) and not isinstance(oom_events, bool) else None,
        cpu_throttling_ratio=float(throttling) if isinstance(throttling, (int, float)) else None,
        cost_per_hour_before=cost_per_hour,
        co2e_grams_per_hour_before=co2e_per_hour,
        proposed_patch=build_patch(rec),
        expected_savings_cost_annual=rec.potential_savings_cost,
        expected_savings_co2e_grams_annual=rec.potential_savings_co2e_grams,
        savings_method=_savings_method(rec),
        rollback_conditions=_rollback_conditions(rec, cpu_usage, memory_usage, proposed),
        generated_by=rec.source.value if hasattr(rec.source, "value") else str(rec.source),
        limit_changes=limit_changes,
        container_kind=_container_kind(summary),
    )


def first_observed_at(
    series: Sequence[CombinedMetric], summary: Any, context: "OptimizationContext", step_seconds: int
) -> Optional[datetime]:
    """First sample of the target inside the evidence window, or None when unknown (H1).

    The OOM baseline of a reliability increase is a rate over the time the
    workload was observed, not over the cluster's data window: a workload created
    40 minutes before the scan was never exposed for the hours before it. With a
    usage summary, the first sample lies in its first observed hour; the observed
    collection steps (``step_count`` x step, counted back from the window end)
    place it within that hour, never outside it (collection gaps or compressed rows
    that overcount steps). Raw series give their first sample time; rollup rows,
    whose timestamp is not a sample time, give nothing. The result never precedes
    the window start nor follows its end.
    """
    start = ensure_utc(context.window_start) if context.window_start is not None else None
    end = ensure_utc(context.window_end) if context.window_end is not None else None
    first: Optional[datetime] = None
    first_hour = getattr(summary, "first_hour", None)
    if isinstance(first_hour, datetime):
        first = ensure_utc(first_hour)
        steps = getattr(summary, "step_count", None)
        if (
            end is not None
            and isinstance(steps, int)
            and not isinstance(steps, bool)
            and steps > 0
            and step_seconds > 0
        ):
            counted_back = end - timedelta(seconds=steps * step_seconds)
            first = min(max(counted_back, first), first + timedelta(hours=1))
    else:
        samples = [
            ensure_utc(metric.timestamp)
            for metric in series
            if not isinstance(metric, BaselineMetric) and metric.timestamp is not None
        ]
        first = min(samples) if samples else None
    if first is None:
        return None
    if start is not None and first < start:
        first = start
    if end is not None and first > end:
        return None
    return first


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _container_kind(summary: Any) -> Optional[Literal["container", "sidecar", "init"]]:
    kind = getattr(summary, "container_kind", None)
    if kind == "sidecar" or kind == "init" or kind == "container":
        return kind
    return None


def _limit_changes(rec: Recommendation, context: "OptimizationContext", summary: Any) -> List[LimitChange]:
    """Limit edits the rightsizing rules deliver with this request change (H1), empty when none."""
    from greenkube.core.optimization.analyzers.rightsizing import planned_limit_changes
    from greenkube.models.workload_usage import POD_LEVEL

    if summary is None or not rec.container_name or context.usage is None:
        return []
    if rec.type == RecommendationType.RIGHTSIZING_CPU:
        resource, proposed = "cpu", rec.recommended_cpu_request_millicores
    elif rec.type == RecommendationType.RIGHTSIZING_MEMORY:
        resource, proposed = "memory", rec.recommended_memory_request_bytes
    else:
        return []
    if not isinstance(proposed, int) or proposed <= 0:
        return []
    target = (summary.namespace, summary.target_kind, summary.target_name)
    pod_summary = context.usage.summaries.get(target, {}).get(POD_LEVEL)
    try:
        return planned_limit_changes(context.config, summary, pod_summary, resource, proposed)
    except Exception as exc:  # advice never breaks a scan; the change then leaves limits alone
        logger.warning("Could not plan limit changes for %s: %s", rec.container_name, exc)
        return []
