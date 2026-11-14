# src/greenkube/verify/attribution.py
"""Attribution groups: one measurement per change, split across its recommendations (C5).

A change can carry several recommendations (the CPU and memory requests of one
or more containers of a workload). The change is measured once, on the
workload, and the measured saving is split:

* by **cost component** when the workload's cost rows carry the CPU/RAM split
  (A4): CPU recommendations share the measured ``cost_cpu`` saving and memory
  recommendations the ``cost_ram`` saving, each in proportion to the request
  reduction it made (millicores or bytes);
* otherwise by **projected share** of the ``allocated_cost`` saving (each
  recommendation's projected annual *allocated* cost saving over the group's
  total, a projected increase weighing nothing; equal shares when nothing was
  projected).

No saving is ever claimed twice: shares within a basis sum to one. Component
claims never exceed the change's net saving: a bundle can cut CPU (``cost_cpu``
-$100/yr) while it raises memory (``cost_ram`` +$80/yr), or a component the
change did not touch can grow. The component shares that save are then scaled
by ``net / sum of component savings`` (the change's measured ``allocated_cost``
saving over the sum of its saving components), so the bundle books the net
$20/yr, never the gross $100/yr; a component that grew keeps its signed,
unclaimed (not realized) estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.verify.model import SavingsEstimate


@dataclass(frozen=True)
class Share:
    record: RecommendationRecord
    basis: str
    share: float
    quantity: str


def _reduction(record: RecommendationRecord) -> float:
    if record.type is RecommendationType.RIGHTSIZING_CPU:
        current = record.current_cpu_request_millicores
        applied = record.actual_cpu_request_millicores or record.recommended_cpu_request_millicores
    elif record.type is RecommendationType.RIGHTSIZING_MEMORY:
        current = record.current_memory_request_bytes
        applied = record.actual_memory_request_bytes or record.recommended_memory_request_bytes
    else:
        return 0.0
    if current is None or applied is None:
        return 0.0
    return max(float(current) - float(applied), 0.0)


def _measured(estimate: Optional[SavingsEstimate]) -> bool:
    return estimate is not None and estimate.rate_per_hour is not None and estimate.aligned_hours > 0


def allocated_projection(record: RecommendationRecord) -> Optional[float]:
    """Projected annual *allocated* cost saving of a recommendation (what a measurement is compared with).

    The Impact Model's ``allocated_cost_annual``; recommendations stored before
    it (or without a projection) fall back to ``potential_savings_cost``.
    """
    impact = record.impact if isinstance(record.impact, dict) else None
    value = impact.get("allocated_cost_annual") if impact is not None else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return record.potential_savings_cost


def component_cap(estimates: Sequence[SavingsEstimate], quantities: Sequence[str]) -> float:
    """Scale (0..1) applied to the saving components so their sum never exceeds the net saving.

    ``1.0`` when the components that save add up to at most the measured net
    ``allocated_cost`` saving; ``net / sum`` when they add up to more (another
    component grew); ``0.0`` when the change saved nothing net.
    """
    by_quantity = {item.quantity: item for item in estimates}
    savings = [
        float(estimate.annualized)
        for quantity in quantities
        if (estimate := by_quantity.get(quantity)) is not None
        and estimate.annualized is not None
        and estimate.annualized > 0
    ]
    gross = math.fsum(savings)
    if gross <= 0:
        return 1.0
    net = by_quantity.get("allocated_cost")
    if net is None or net.annualized is None:
        return 0.0
    return max(0.0, min(1.0, float(net.annualized) / gross))


def _weights(records: Sequence[RecommendationRecord], weight) -> List[float]:
    raw = [max(weight(record), 0.0) for record in records]
    total = sum(raw)
    if total <= 0:
        return [1.0 / len(records)] * len(records)
    return [value / total for value in raw]


def allocate(records: Sequence[RecommendationRecord], estimates: Sequence[SavingsEstimate]) -> List[Share]:
    """Split a change measurement across its recommendations."""
    members = [record for record in records if record.id is not None]
    if not members:
        return []
    by_quantity: Dict[str, SavingsEstimate] = {item.quantity: item for item in estimates}
    cpu = [record for record in members if record.type is RecommendationType.RIGHTSIZING_CPU]
    memory = [record for record in members if record.type is RecommendationType.RIGHTSIZING_MEMORY]
    others = [record for record in members if record not in cpu and record not in memory]
    components = (
        not others
        and (not cpu or _measured(by_quantity.get("cost_cpu")))
        and (not memory or _measured(by_quantity.get("cost_ram")))
    )
    if components:
        shares: List[Share] = []
        groups = [(group, quantity) for group, quantity in ((cpu, "cost_cpu"), (memory, "cost_ram")) if group]
        cap = component_cap(estimates, ("cost_cpu", "cost_ram"))
        for group, quantity in groups:
            estimate = by_quantity.get(quantity)
            saves = estimate is not None and estimate.annualized is not None and estimate.annualized > 0
            scale = cap if saves else 1.0
            for record, share in zip(group, _weights(group, _reduction)):
                shares.append(Share(record=record, basis=quantity, share=share * scale, quantity=quantity))
        return shares
    basis = "projected_share" if len(members) > 1 else "total"
    weights = _weights(members, lambda record: float(allocated_projection(record) or 0.0))
    return [
        Share(record=record, basis=basis, share=share, quantity="allocated_cost")
        for record, share in zip(members, weights)
    ]
