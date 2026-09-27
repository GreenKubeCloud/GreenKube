# src/greenkube/core/optimization/evidence.py
"""Builds review-grade evidence blocks for generated recommendations."""

import logging
from typing import TYPE_CHECKING, List, Optional, Sequence

from greenkube.core.optimization.statistics import latest_request_value, percentile, usage_stats
from greenkube.models.evidence import (
    ProposedChange,
    RecommendationEvidence,
    ResourceSnapshot,
    RollbackCondition,
    UtilizationStats,
)
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

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
        return [m for m in context.metrics if m.node == rec.target_node]
    if rec.owner_kind and rec.owner_name:
        key = (rec.namespace or "", rec.owner_kind, rec.owner_name)
        return context.target_series().get(key, [])
    if rec.pod_name:
        for key, series in context.target_series().items():
            if key[0] == rec.namespace and key[2] == rec.pod_name:
                return series
        return [m for m in context.metrics if m.namespace == rec.namespace and m.pod_name == rec.pod_name]
    if rec.namespace:
        return [m for m in context.metrics if m.namespace == rec.namespace]
    return []


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
    if rec.type in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY):
        return "request_reduction_ratio"
    if rec.type in (RecommendationType.ZOMBIE_POD, RecommendationType.IDLE_NAMESPACE):
        return "observed_cost_annualized"
    if rec.type in (RecommendationType.ORPHANED_PERSISTENT_VOLUME, RecommendationType.ORPHANED_LOAD_BALANCER):
        return "opencost_or_flat_estimate"
    if rec.type == RecommendationType.CARBON_AWARE_SCHEDULING:
        return "high_carbon_share"
    return "unknown"


def build_patch(rec: Recommendation) -> Optional[dict]:
    """Builds a machine-readable action plan for the future GitOps patcher."""
    if rec.type not in (RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY):
        return None
    if not rec.owner_kind or not rec.owner_name:
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

    return {
        "kind": rec.owner_kind,
        "namespace": rec.namespace,
        "name": rec.owner_name,
        "operations": operations,
    }


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
    cost_per_hour = (total_cost / window * 3600) if window else None
    co2e_per_hour = (total_co2e / window * 3600) if window else None

    sample_count = max((cpu_usage.sample_count if cpu_usage else 0), (memory_usage.sample_count if memory_usage else 0))
    coverage = max(
        cpu_usage.coverage_ratio if cpu_usage else 0.0,
        memory_usage.coverage_ratio if memory_usage else 0.0,
    )

    return RecommendationEvidence(
        observation_window_start=context.window_start,
        observation_window_end=context.window_end,
        observation_window_seconds=window,
        sample_count=sample_count,
        coverage_ratio=coverage or 1.0,
        current=current,
        proposed=proposed,
        changes=_changes(rec, current, proposed),
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        restart_count=restart_count,
        oom_events=None,
        cost_per_hour_before=cost_per_hour,
        co2e_grams_per_hour_before=co2e_per_hour,
        proposed_patch=build_patch(rec),
        expected_savings_cost_annual=rec.potential_savings_cost,
        expected_savings_co2e_grams_annual=rec.potential_savings_co2e_grams,
        savings_method=_savings_method(rec),
        rollback_conditions=_rollback_conditions(rec, cpu_usage, memory_usage, proposed),
        generated_by=rec.source.value if hasattr(rec.source, "value") else str(rec.source),
    )
