# src/greenkube/core/opportunity.py
"""The projected opportunity of active recommendations, computed once (IMP-07).

The opportunity is computed here, server side, and every page renders this
one figure (one stored recommendation per target and capability, so nothing
overlaps):

- the CPU and memory request changes of the workloads add up;
- asset deletions (orphaned volumes and LoadBalancers) are billed per asset
  and add up;
- request increases (the reliability lane) are a cost, reported apart and never
  netted into the opportunity.

Each metric (billable cost, allocated cost, CO2e) is summed on its own.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Tuple

from pydantic import BaseModel, Field

from greenkube.models.metrics import RecommendationRecord

#: Label every page shows next to the opportunity.
OPPORTUNITY_LABEL = "Projected (not additive across overlapping recommendations)"

#: Metrics summed independently: billable cost, allocated cost, CO2e grams.
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

    return is_reliability_increase(record) or metrics[0] < 0 or metrics[1] < 0


def _add(left: _Metrics, right: _Metrics) -> _Metrics:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def _positive(metrics: _Metrics) -> _Metrics:
    return (max(metrics[0], 0.0), max(metrics[1], 0.0), max(metrics[2], 0.0))


def opportunity_of(records: Iterable[RecommendationRecord]) -> OpportunitySummary:
    """De-duplicated projected opportunity of ``records`` (the active recommendations)."""
    total = _ZERO
    reliability = 0.0
    reliability_count = 0
    count = 0
    for record in records:
        metrics = _metrics(record)
        if _is_reliability(record, metrics):
            # The allocated cost (the billable one is 0 on fixed nodes, where a fix still costs capacity).
            reliability += max(-metrics[1], -metrics[0], 0.0)
            reliability_count += 1
            continue
        count += 1
        total = _add(total, _positive(metrics))
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
