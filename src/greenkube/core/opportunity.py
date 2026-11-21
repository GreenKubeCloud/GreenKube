# src/greenkube/core/opportunity.py
"""The projected opportunity of active recommendations, de-duplicated once (IMP-07).

Active recommendations overlap: a namespace deletion contains the workloads a
rightsizing or a zombie-pod deletion also targets, an off-peak schedule and a
rightsizing of one workload remove the same hours, and a node removal releases
the node-hours its pods' rightsizing also counts. Summing every record
overstates what can be achieved, so the opportunity is computed here, server
side, and every page renders this one figure:

- per workload, the CPU and memory requests (and other additive changes of the
  workload) add up, and the alternatives that remove the workload's hours (an
  off-peak schedule, a zombie-pod deletion) count as ``max(alternative, sum)``;
- per namespace, ``max(namespace deletion, sum of its workloads)``;
- node removals and pod-level savings count the same node-hours:
  ``max(node removals, pod-level savings)``;
- asset deletions (orphaned volumes and LoadBalancers) are billed per asset
  and add up;
- request increases (the reliability lane) are a cost, reported apart and never
  netted into the opportunity.

Each metric (billable cost, allocated cost, CO2e) is de-duplicated on its own.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional, Tuple

from pydantic import BaseModel, Field

from greenkube.models.metrics import RecommendationRecord, RecommendationType

#: Label every page shows next to the opportunity.
OPPORTUNITY_LABEL = "Projected (not additive across overlapping recommendations)"

_NAMESPACE_TYPES = {RecommendationType.IDLE_NAMESPACE.value}
_ALTERNATIVE_TYPES = {RecommendationType.OFF_PEAK_SCALING.value, RecommendationType.ZOMBIE_POD.value}
_NODE_TYPES = {RecommendationType.OVERPROVISIONED_NODE.value, RecommendationType.UNDERUTILIZED_NODE.value}
_ASSET_TYPES = {
    RecommendationType.ORPHANED_PERSISTENT_VOLUME.value,
    RecommendationType.ORPHANED_LOAD_BALANCER.value,
}

#: Metrics de-duplicated independently: billable cost, allocated cost, CO2e grams.
_Metrics = Tuple[float, float, float]
_ZERO: _Metrics = (0.0, 0.0, 0.0)


class OpportunitySummary(BaseModel):
    """De-duplicated projected opportunity of the active recommendations (IMP-07)."""

    cost_annual: float = Field(0.0, description="Projected billable cost saving per year, de-duplicated.")
    allocated_cost_annual: float = Field(
        0.0, description="Projected allocated cost saving per year (Impact Model allocated basis), de-duplicated."
    )
    co2e_grams_annual: float = Field(0.0, description="Projected CO2e saving per year in grams, de-duplicated.")
    reliability_cost_annual: float = Field(
        0.0, description="Projected yearly cost of the reliability fixes (request increases), positive, never netted."
    )
    reliability_count: int = Field(0, description="Active reliability recommendations (request increases).")
    recommendation_count: int = Field(0, description="Active savings recommendations behind the opportunity.")
    label: str = OPPORTUNITY_LABEL


def _type(record: RecommendationRecord) -> str:
    value = record.type
    return str(value.value if hasattr(value, "value") else value)


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0.0
    return float(value)


def _metrics(record: RecommendationRecord) -> _Metrics:
    billable = _finite(record.potential_savings_cost)
    impact = record.impact if isinstance(record.impact, dict) else None
    allocated_value = impact.get("allocated_cost_annual") if impact is not None else None
    allocated = _finite(allocated_value) if allocated_value is not None else billable
    return billable, allocated, _finite(record.potential_savings_co2e_grams)


def _is_reliability(record: RecommendationRecord, metrics: _Metrics) -> bool:
    from greenkube.verify.strategy import is_reliability_increase

    return is_reliability_increase(record) or metrics[0] < 0


def _add(left: _Metrics, right: _Metrics) -> _Metrics:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def _max(left: _Metrics, right: _Metrics) -> _Metrics:
    return (max(left[0], right[0]), max(left[1], right[1]), max(left[2], right[2]))


def _positive(metrics: _Metrics) -> _Metrics:
    return (max(metrics[0], 0.0), max(metrics[1], 0.0), max(metrics[2], 0.0))


def opportunity_of(records: Iterable[RecommendationRecord]) -> OpportunitySummary:
    """De-duplicated projected opportunity of ``records`` (the active recommendations)."""
    namespace_level: Dict[str, _Metrics] = defaultdict(lambda: _ZERO)
    additive: Dict[Tuple[str, str, str], _Metrics] = defaultdict(lambda: _ZERO)
    alternatives: Dict[Tuple[str, str, str], _Metrics] = defaultdict(lambda: _ZERO)
    nodes = _ZERO
    assets = _ZERO
    reliability = 0.0
    reliability_count = 0
    count = 0
    for record in records:
        metrics = _metrics(record)
        if _is_reliability(record, metrics):
            reliability += max(-metrics[0], 0.0)
            reliability_count += 1
            continue
        count += 1
        metrics = _positive(metrics)
        kind = _type(record)
        namespace = record.namespace or ""
        if kind in _ASSET_TYPES:
            assets = _add(assets, metrics)
        elif kind in _NODE_TYPES:
            nodes = _add(nodes, metrics)
        elif kind in _NAMESPACE_TYPES:
            namespace_level[namespace] = _max(namespace_level[namespace], metrics)
        else:
            workload = (namespace, record.owner_kind or "Pod", record.owner_name or record.pod_name or "")
            if kind in _ALTERNATIVE_TYPES:
                alternatives[workload] = _max(alternatives[workload], metrics)
            else:
                additive[workload] = _add(additive[workload], metrics)
    workloads: Dict[str, _Metrics] = defaultdict(lambda: _ZERO)
    for key in set(additive) | set(alternatives):
        workloads[key[0]] = _add(workloads[key[0]], _max(additive.get(key, _ZERO), alternatives.get(key, _ZERO)))
    pods = _ZERO
    for namespace in set(workloads) | set(namespace_level):
        pods = _add(pods, _max(workloads.get(namespace, _ZERO), namespace_level.get(namespace, _ZERO)))
    total = _add(assets, _max(pods, nodes))
    return OpportunitySummary(
        cost_annual=total[0],
        allocated_cost_annual=total[1],
        co2e_grams_annual=total[2],
        reliability_cost_annual=reliability,
        reliability_count=reliability_count,
        recommendation_count=count,
    )


def optional_opportunity(records: Optional[Iterable[RecommendationRecord]]) -> Optional[OpportunitySummary]:
    """:func:`opportunity_of`, or ``None`` when the active recommendations were not read."""
    return None if records is None else opportunity_of(records)
