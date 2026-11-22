# src/greenkube/verify/deletion.py
"""Measurement of resource deletions (roadmap H6, C3): ``ResourceDeletionStrategy``.

Deleting an orphaned LoadBalancer Service or PersistentVolume releases the asset's whole
allocated rate (Impact Model realization 1: the same model as the projection). Once apply
detection has verified that the resource is absent, the realized saving is the rate the
resource was charged before the change:

* the mean trusted cost of the resource in the baseline window when GreenKube observed it
  (``pre_window_mean``, rate source ``observed``);
* otherwise the allocated rate frozen with the recommendation at decision time
  (``allocated_rate_at_decision``), **only when that rate has a trusted provenance**: the
  OpenCost or Kubecost per-asset cost, or a pricing-catalog asset price (the projection's
  ``assumptions.cost_source``). A configured default (``LOAD_BALANCER_COST_PER_MONTH``,
  ``STORAGE_COST_PER_GIB_MONTH``: ``custom_rate``), no cost (``none``) or an unknown
  provenance is never booked as measured: the deletion is ``inconclusive`` and its saving
  stays a projection.

The rate source is recorded on the estimate (``rate_source``), in the counterfactual
parameters and in the measurement's ``strategy_version`` (``1.1:<method>:<source>``), which
the savings ledger v2 carries as the entries' ``method_version``.

A resource still observed after the change is ``not_realized``; a change whose absence
was never verified against the live cluster (``rollout_completed_source`` other than
``resource_absent``) or a missing rate is ``inconclusive``; a severe confounder is
``contaminated``. The interval is degenerate: absence is a fact, not a sample, so there
is no counterfactual noise to bootstrap.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence

from greenkube.impact.types import HOURS_PER_YEAR as IMPACT_HOURS_PER_YEAR
from greenkube.models.provenance import TRUSTED_COST_SOURCES
from greenkube.utils.time import ensure_utc
from greenkube.verify.confounders import is_contaminated
from greenkube.verify.load_driver import ResolvedLoad
from greenkube.verify.model import (
    ChangeEvent,
    ChangeOutcome,
    Confounder,
    HourBucket,
    SavingsEstimate,
    TimeWindow,
    WindowPlan,
)

if TYPE_CHECKING:
    from greenkube.verify.strategy import MeasurementSettings, StrategyResult

#: Same constant as ``greenkube.verify.strategy`` (which registers this strategy, so it is not imported here).
HOURS_PER_YEAR = float(IMPACT_HOURS_PER_YEAR)  # one annualisation constant with the Impact Model (MEA-13)
METHOD_PRE_WINDOW = "pre_window_mean"
METHOD_ALLOCATED_RATE = "allocated_rate_at_decision"
#: Rate source of a rate measured on the resource's own trusted cost rows before the change.
RATE_SOURCE_OBSERVED = "observed"
#: Rate source when the recommendations do not say where their frozen rate came from.
RATE_SOURCE_UNKNOWN = "unknown"


def frozen_rate_source(sources: Sequence[str]) -> str:
    """One provenance for the frozen rate of a change's recommendations (``mixed`` when they differ)."""
    values = sorted({str(item).strip().lower() or RATE_SOURCE_UNKNOWN for item in sources})
    if not values:
        return RATE_SOURCE_UNKNOWN
    return values[0] if len(values) == 1 else "mixed"


def is_trusted_rate_source(source: str) -> bool:
    """Whether a frozen asset rate may back a measured saving (an allocation engine or a price catalog)."""
    return source in {str(item) for item in TRUSTED_COST_SOURCES}


#: The rollout completion source apply detection (and manual mode) records once it read the resource as absent.
COMPLETION_RESOURCE_ABSENT = "resource_absent"


class ResourceDeletionStrategy:
    """Measures a resource deletion: the released allocated rate once the resource is absent."""

    key = "resource_deletion"
    version = "1.2"
    primary = "allocated_cost"

    def required_windows(self, change: ChangeEvent, settings: MeasurementSettings) -> WindowPlan:
        decision = ensure_utc(change.baseline_window_end)
        post_start = ensure_utc(change.rollout_completed_at) + timedelta(hours=settings.warmup_hours)
        return WindowPlan(
            pre=TimeWindow(start=decision - timedelta(days=settings.pre_window_days), end=decision),
            post=TimeWindow(start=post_start, end=post_start + timedelta(days=settings.post_window_days)),
            warmup_hours=settings.warmup_hours,
            min_coverage=settings.min_coverage,
        )

    def estimate(
        self,
        change: ChangeEvent,
        plan: WindowPlan,
        pre: Sequence[HourBucket],
        post: Sequence[HourBucket],
        *,
        load: Optional[ResolvedLoad],
        confounders: Sequence[Confounder],
        projections: Mapping[str, Optional[float]],
        settings: MeasurementSettings,
        seed_parts: Sequence[object],
        rate_sources: Sequence[str] = (),
        price_card: Optional[Mapping[str, Any]] = None,
    ) -> StrategyResult:
        from greenkube.verify.strategy import StrategyResult

        del plan, load, settings, seed_parts, price_card  # a deletion releases the whole rate
        absence_verified = change.rollout_completed_source == COMPLETION_RESOURCE_ABSENT
        projected = projections.get("allocated_cost")
        base: Dict[str, Any] = {
            "quantity": "allocated_cost",
            "unit": "currency",
            "claimed": True,
            "projected_annual": projected,
        }
        observed_after = [bucket for bucket in post if bucket.coverage > 0]
        pre_costs = [bucket.cost for bucket in pre if bucket.cost is not None and bucket.cost_coverage > 0]
        rate: Optional[float] = None
        method: Optional[str] = None
        rate_source: Optional[str] = None
        untrusted_rate = False
        if pre_costs:
            rate, method, rate_source = math.fsum(pre_costs) / len(pre_costs), METHOD_PRE_WINDOW, RATE_SOURCE_OBSERVED
        elif isinstance(projected, (int, float)) and math.isfinite(projected) and projected > 0:
            method, rate_source = METHOD_ALLOCATED_RATE, frozen_rate_source(rate_sources)
            if is_trusted_rate_source(rate_source):
                rate = float(projected) / HOURS_PER_YEAR
            else:
                untrusted_rate = True
        base["rate_source"] = rate_source

        reasons: List[str] = []
        if observed_after:
            estimate = SavingsEstimate(
                **base,
                method=method,
                outcome=ChangeOutcome.NOT_REALIZED,
                reason=f"the resource is still observed in {len(observed_after)} post-change hour(s)",
            )
        elif not absence_verified:
            estimate = SavingsEstimate(
                **base,
                method=method,
                outcome=ChangeOutcome.INCONCLUSIVE,
                reason="the absence of the resource was never verified in the cluster, so no saving is claimed",
            )
        elif untrusted_rate:
            estimate = SavingsEstimate(
                **base,
                method=method,
                outcome=ChangeOutcome.INCONCLUSIVE,
                reason=(
                    f"the frozen rate of the deleted resource comes from {rate_source} pricing, not from a trusted "
                    "allocation (OpenCost, Kubecost) or price catalog: the saving stays a projection"
                ),
            )
        elif rate is None:
            estimate = SavingsEstimate(
                **base,
                outcome=ChangeOutcome.INCONCLUSIVE,
                reason="no trusted cost of the deleted resource before the change and no allocated rate",
            )
        else:
            annual = rate * HOURS_PER_YEAR
            estimate = SavingsEstimate(
                **base,
                rate_per_hour=rate,
                ci_low=rate,
                ci_high=rate,
                counterfactual_per_hour=rate,
                actual_per_hour=0.0,
                annualized=annual,
                annualized_ci_low=annual,
                annualized_ci_high=annual,
                coverage=1.0,
                aligned_hours=len(post),
                method=method,
                outcome=ChangeOutcome.REALIZED,
                reason=(
                    "the resource is absent: its allocated rate is released "
                    f"({'measured before the change' if method == METHOD_PRE_WINDOW else 'allocated rate at decision'}"
                    f", {rate_source})"
                ),
                projection_accuracy=(annual / projected) if projected else None,
            )
        if is_contaminated(confounders):
            outcome = ChangeOutcome.CONTAMINATED
            reasons = [item.detail for item in confounders if item.severity == "severe"]
        else:
            outcome = estimate.outcome
            reasons = [estimate.reason] if estimate.reason else []
        return StrategyResult(
            estimates=[estimate],
            outcome=outcome,
            reasons=reasons,
            coverage=estimate.coverage,
            counterfactual_params={
                "model": method or "none",
                "pre_cost_hours": len(pre_costs),
                "rate_source": rate_source,
            },
            strategy_version=f"{self.version}:{method or 'none'}:{rate_source or 'none'}",
        )
