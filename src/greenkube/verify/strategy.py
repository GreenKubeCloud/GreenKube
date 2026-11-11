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

from greenkube.impact.types import CO2E_COMPONENTS, MEASURED_CO2E_COMPONENTS
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
from greenkube.verify.regression import METHOD_OLS, OlsCounterfactual, residual_bootstrap
from greenkube.verify.series import co2e_series, has_embodied, quantity_series, window_hours
from greenkube.verify.statistics import moving_block_bootstrap, outcome_rule, seed_for

HOURS_PER_YEAR = 365.25 * 24
#: The hour-of-week MVP counterfactual (C3), the fallback of the regression.
MVP_METHOD = "hour_of_week_mean"
#: Projection key of the CO2e of every component, embodied included (component matching).
CO2E_WITH_EMBODIED = "co2e_g_with_embodied"


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
    * ``co2e_g`` — CO2e of the workload as the Impact Model allocates it
      (idle power by CPU reservation, memory power by memory reservation,
      dynamic and GPU power by usage, embodied emissions by reservation), so
      request changes move it. Embodied emissions are included only when the
      baseline snapshot carries them (snapshots frozen before they were
      measured do not); ``components`` lists what was measured, and the
      projection it is compared with covers the same components;
    * ``embodied_co2e_g`` — the embodied part alone (reported).

    Each quantity's counterfactual is the load regression (C3 v2,
    :mod:`greenkube.verify.regression`) when a load driver exists and the fit's
    diagnostics pass, otherwise the hour-of-week MVP; ``method`` and the
    diagnostics are recorded.
    """

    key = "request_rightsizing"
    version = "2.0"
    primary = "allocated_cost"

    QUANTITIES = (
        QuantitySpec("allocated_cost", "currency", True, "allocated cost"),
        QuantitySpec("cost_cpu", "currency", False, "allocated CPU cost"),
        QuantitySpec("cost_ram", "currency", False, "allocated RAM cost"),
        QuantitySpec("core_hours", "core-hours", False, "requested CPU cores"),
        QuantitySpec("gib_hours", "GiB-hours", False, "requested memory"),
        QuantitySpec("co2e_g", "gCO2e", True, "allocated CO2e"),
        QuantitySpec("embodied_co2e_g", "gCO2e", False, "embodied CO2e"),
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
        methods: Dict[str, str] = {}
        elasticities: Dict[str, float] = {}
        matched: Dict[str, int] = {}
        diagnostics: Dict[str, Any] = {}
        embodied = has_embodied(pre) and has_embodied(post)
        for spec in self.QUANTITIES:
            fitted = self._estimate_quantity(
                spec,
                pre,
                post,
                load,
                expected_hours,
                projections,
                settings,
                seed_parts,
                embodied=embodied,
            )
            estimates.append(fitted.estimate)
            if fitted.mvp is not None:
                elasticities[spec.quantity] = round(fitted.mvp.elasticity, 6)
                matched[spec.quantity] = len(fitted.mvp.means)
            if fitted.estimate.method is not None:
                methods[spec.quantity] = fitted.estimate.method
            if fitted.diagnostics is not None:
                diagnostics[spec.quantity] = fitted.diagnostics
        params: Dict[str, Any] = {
            "model": methods.get(self.primary, MVP_METHOD),
            "methods": methods,
            "elasticity": elasticities,
            "matched_hours_of_week": matched,
            "diagnostics": diagnostics,
            "co2e_components": list(_co2e_components(embodied)),
            "block_hours": 24,
            "bootstrap_samples": settings.bootstrap_samples,
            "epsilon_ratio": settings.epsilon_ratio,
        }

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
        projections: Mapping[str, Optional[float]],
        settings: MeasurementSettings,
        seed_parts: Sequence[object],
        *,
        embodied: bool = False,
    ) -> "_Fitted":
        components: List[str] = []
        projected = projections.get(spec.quantity)
        if spec.quantity == "co2e_g":
            # Component matching: the measured CO2e covers embodied emissions only when both
            # windows carry them (snapshots frozen before they were measured do not).
            components = list(_co2e_components(embodied))
            pre_values = co2e_series(pre, embodied=embodied)
            post_values = co2e_series(post, embodied=embodied)
            projected = projections.get(CO2E_WITH_EMBODIED) if embodied else projections.get("co2e_g")
        else:
            pre_values = quantity_series(pre, spec.quantity)
            post_values = quantity_series(post, spec.quantity)
        base: Dict[str, Any] = {
            "quantity": spec.quantity,
            "unit": spec.unit,
            "claimed": spec.claimed,
            "projected_annual": projected,
            "components": components,
        }
        if not pre_values or not post_values:
            missing = "baseline" if not pre_values else "post-change"
            return _Fitted(
                SavingsEstimate(
                    **base,
                    outcome=ChangeOutcome.INCONCLUSIVE,
                    reason=f"no trusted {spec.description} in the {missing} window",
                )
            )
        mvp = HourOfWeekCounterfactual.fit(pre_values, load.pre if load is not None else None)
        diagnostics: Optional[Dict[str, Any]] = None
        ols: Optional[OlsCounterfactual] = None
        if load is not None:
            post_load = {hour: load.post[hour] for hour in post_values if load.post.get(hour) is not None}
            candidate = OlsCounterfactual.fit(pre_values, load.pre, post_load)
            diagnostics = candidate.diagnostics.as_dict()
            if candidate.diagnostics.passed:
                ols = candidate
            else:
                diagnostics["fallback"] = MVP_METHOD
        seed = seed_for(*seed_parts, spec.quantity)
        if ols is not None and load is not None:
            method = METHOD_OLS
            aligned = ols.align(post_values, load.post)
            interval = residual_bootstrap(
                [item.saving for item in aligned],
                [residual for *_, residual in ols.baseline],
                ols.residual_weights(aligned, load.post),
                dof=ols.diagnostics.dof,
                seed=seed,
                samples=settings.bootstrap_samples,
            )
            elasticity = ols.elasticity
        else:
            method = MVP_METHOD
            aligned = align(mvp, post_values, load.post if load is not None else None)
            interval = moving_block_bootstrap(
                [item.saving for item in aligned], seed=seed, samples=settings.bootstrap_samples
            )
            elasticity = mvp.elasticity
        coverage = len(aligned) / expected_hours
        if not aligned:
            return _Fitted(
                SavingsEstimate(
                    **base,
                    elasticity=round(elasticity, 6),
                    method=method,
                    outcome=ChangeOutcome.INCONCLUSIVE,
                    reason=f"no post-change hour of {spec.description} matches a baseline hour of the week",
                ),
                mvp,
                diagnostics,
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
        return _Fitted(
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
                elasticity=round(elasticity, 6),
                method=method,
                outcome=outcome,
                reason=reason,
                projection_accuracy=(annualized / projected) if projected else None,
            ),
            mvp,
            diagnostics,
        )


@dataclass
class _Fitted:
    estimate: SavingsEstimate
    mvp: Optional[HourOfWeekCounterfactual] = None
    diagnostics: Optional[Dict[str, Any]] = None


def _co2e_components(embodied: bool) -> tuple[str, ...]:
    return CO2E_COMPONENTS if embodied else MEASURED_CO2E_COMPONENTS


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
