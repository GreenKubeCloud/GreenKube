# src/greenkube/verify/strategy.py
"""Savings measurement strategies (C3, C4).

A strategy turns a change, its frozen baseline and the post-change
observations into per-quantity :class:`~greenkube.verify.model.SavingsEstimate`
values. Only :class:`RequestRightsizingStrategy` exists today (container
resource changes, ``action_kind="container_resources"``); other action kinds
register their own strategy by key.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from greenkube.utils.time import ensure_utc
from greenkube.verify.confounders import is_contaminated
from greenkube.verify.counterfactual import HourOfWeekCounterfactual, align
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
from greenkube.verify.series import quantity_series, window_hours
from greenkube.verify.statistics import moving_block_bootstrap, outcome_rule, seed_for

HOURS_PER_YEAR = 365.25 * 24


@dataclass(frozen=True)
class MeasurementSettings:
    """Tunables of a change measurement (``CHANGE_MEASUREMENT_*`` settings)."""

    pre_window_days: float = 7.0
    post_window_days: float = 7.0
    warmup_hours: float = 1.0
    min_coverage: float = 0.8
    bootstrap_samples: int = 1000
    epsilon_ratio: float = 0.02

    @classmethod
    def from_config(cls, config: Any) -> "MeasurementSettings":
        return cls(
            pre_window_days=float(getattr(config, "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS", 7.0)),
            post_window_days=float(getattr(config, "CHANGE_MEASUREMENT_POST_WINDOW_DAYS", 7.0)),
            warmup_hours=float(getattr(config, "CHANGE_MEASUREMENT_WARMUP_HOURS", 1.0)),
            min_coverage=float(getattr(config, "CHANGE_MEASUREMENT_MIN_COVERAGE", 0.8)),
            bootstrap_samples=int(getattr(config, "CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES", 1000)),
            epsilon_ratio=float(getattr(config, "CHANGE_MEASUREMENT_EPSILON_RATIO", 0.02)),
        )


@dataclass(frozen=True)
class QuantitySpec:
    quantity: str
    unit: str
    claimed: bool
    description: str


@dataclass
class StrategyResult:
    estimates: List[SavingsEstimate]
    outcome: ChangeOutcome
    reasons: List[str]
    coverage: float
    counterfactual_params: Dict[str, Any] = field(default_factory=dict)


class SavingsMeasurementStrategy(Protocol):
    key: str
    version: str

    def required_windows(self, change: ChangeEvent, settings: MeasurementSettings) -> WindowPlan: ...

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
    ) -> StrategyResult: ...


class RequestRightsizingStrategy:
    """Measures a change of container requests (C3 MVP).

    Quantities, all per hour and signed (positive = saving):

    * ``allocated_cost`` — trusted allocated cost of the workload (claimed;
      decides the outcome);
    * ``cost_cpu`` / ``cost_ram`` — its CPU and RAM components (used to split
      the saving between the change's recommendations, C5);
    * ``core_hours`` / ``gib_hours`` — requested capacity freed (reported);
    * ``co2e_g`` — usage-based CO2e: reported, never claimed, because a
      request change does not move the usage-based carbon model.
    """

    key = "request_rightsizing"
    version = "1.0"
    primary = "allocated_cost"

    QUANTITIES = (
        QuantitySpec("allocated_cost", "currency", True, "allocated cost"),
        QuantitySpec("cost_cpu", "currency", False, "allocated CPU cost"),
        QuantitySpec("cost_ram", "currency", False, "allocated RAM cost"),
        QuantitySpec("core_hours", "core-hours", False, "requested CPU cores"),
        QuantitySpec("gib_hours", "GiB-hours", False, "requested memory"),
        QuantitySpec("co2e_g", "gCO2e", False, "usage-based CO2e"),
    )

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
    ) -> StrategyResult:
        expected_hours = len(window_hours(plan.post)) or 1
        estimates: List[SavingsEstimate] = []
        params: Dict[str, Any] = {"model": "hour_of_week_mean", "elasticity": {}, "matched_hours_of_week": {}}
        for spec in self.QUANTITIES:
            estimate, model = self._estimate_quantity(
                spec, pre, post, load, expected_hours, projections.get(spec.quantity), settings, seed_parts
            )
            estimates.append(estimate)
            if model is not None:
                params["elasticity"][spec.quantity] = round(model.elasticity, 6)
                params["matched_hours_of_week"][spec.quantity] = len(model.means)
        params["block_hours"] = 24
        params["bootstrap_samples"] = settings.bootstrap_samples
        params["epsilon_ratio"] = settings.epsilon_ratio

        primary = next(item for item in estimates if item.quantity == self.primary)
        reasons: List[str] = []
        if is_contaminated(confounders):
            outcome = ChangeOutcome.CONTAMINATED
            reasons = [item.detail for item in confounders if item.severity == "severe"]
        else:
            outcome = primary.outcome
            if primary.reason:
                reasons.append(primary.reason)
        return StrategyResult(
            estimates=estimates,
            outcome=outcome,
            reasons=reasons,
            coverage=primary.coverage,
            counterfactual_params=params,
        )

    @staticmethod
    def _estimate_quantity(
        spec: QuantitySpec,
        pre: Sequence[HourBucket],
        post: Sequence[HourBucket],
        load: Optional[ResolvedLoad],
        expected_hours: int,
        projected: Optional[float],
        settings: MeasurementSettings,
        seed_parts: Sequence[object],
    ) -> tuple[SavingsEstimate, Optional[HourOfWeekCounterfactual]]:
        pre_values = quantity_series(pre, spec.quantity)
        post_values = quantity_series(post, spec.quantity)
        base = {"quantity": spec.quantity, "unit": spec.unit, "claimed": spec.claimed, "projected_annual": projected}
        if not pre_values or not post_values:
            missing = "baseline" if not pre_values else "post-change"
            return (
                SavingsEstimate(
                    **base,
                    outcome=ChangeOutcome.INCONCLUSIVE,
                    reason=f"no trusted {spec.description} in the {missing} window",
                ),
                None,
            )
        model = HourOfWeekCounterfactual.fit(pre_values, load.pre if load is not None else None)
        aligned = align(model, post_values, load.post if load is not None else None)
        coverage = len(aligned) / expected_hours
        if not aligned:
            return (
                SavingsEstimate(
                    **base,
                    elasticity=model.elasticity,
                    outcome=ChangeOutcome.INCONCLUSIVE,
                    reason=f"no post-change hour of {spec.description} matches a baseline hour of the week",
                ),
                model,
            )
        interval = moving_block_bootstrap(
            [item.saving for item in aligned],
            seed=seed_for(*seed_parts, spec.quantity),
            samples=settings.bootstrap_samples,
        )
        counterfactual = math.fsum(item.counterfactual for item in aligned) / len(aligned)
        actual = math.fsum(item.actual for item in aligned) / len(aligned)
        epsilon = settings.epsilon_ratio * abs(counterfactual)
        if coverage < settings.min_coverage:
            outcome = ChangeOutcome.INCONCLUSIVE
            reason = f"aligned coverage {coverage:.0%} of the post-window is below {settings.min_coverage:.0%}"
        else:
            outcome = outcome_rule(interval.low, interval.high, epsilon)
            reason = _reason(spec, outcome, interval.low, interval.high, epsilon)
        annualized = interval.mean * HOURS_PER_YEAR
        return (
            SavingsEstimate(
                **base,
                rate_per_hour=interval.mean,
                ci_low=interval.low,
                ci_high=interval.high,
                counterfactual_per_hour=counterfactual,
                actual_per_hour=actual,
                annualized=annualized,
                annualized_ci_low=interval.low * HOURS_PER_YEAR if interval.low is not None else None,
                annualized_ci_high=interval.high * HOURS_PER_YEAR if interval.high is not None else None,
                coverage=round(coverage, 4),
                aligned_hours=len(aligned),
                elasticity=round(model.elasticity, 6),
                outcome=outcome,
                reason=reason,
                projection_accuracy=(annualized / projected) if projected else None,
            ),
            model,
        )


def _reason(
    spec: QuantitySpec, outcome: ChangeOutcome, low: Optional[float], high: Optional[float], epsilon: float
) -> str:
    if low is None or high is None:
        return f"{spec.description}: too few aligned hours for an interval"
    interval = f"90% CI [{low:+.6g}, {high:+.6g}] per hour"
    if outcome is ChangeOutcome.REALIZED:
        return f"{spec.description} saving realized: {interval} is above zero"
    if outcome is ChangeOutcome.NOT_REALIZED:
        return f"{spec.description} saving not realized: {interval} is at most the noise floor {epsilon:.6g}"
    return f"{spec.description} saving inconclusive: {interval} straddles the noise floor {epsilon:.6g}"


#: Strategies by the ``action_kind`` of the change (H0 registry keys).
STRATEGIES: Dict[str, SavingsMeasurementStrategy] = {"container_resources": RequestRightsizingStrategy()}


def strategy_for(action_kind: str) -> Optional[SavingsMeasurementStrategy]:
    return STRATEGIES.get(action_kind)
