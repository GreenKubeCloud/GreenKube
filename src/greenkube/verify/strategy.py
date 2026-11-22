# src/greenkube/verify/strategy.py
"""Savings measurement strategies (C3, C4).

A strategy turns a change, its frozen baseline and the post-change
observations into per-quantity :class:`~greenkube.verify.model.SavingsEstimate`
values. Only :class:`RequestRightsizingStrategy` exists today (container
resource changes, ``action_kind="container_resources"``); other action kinds
register their own strategy by key.

A request **increase** (H1) is a reliability fix: its cost is measured like any
request change (a negative saving), and its success is the drop of the OOM-kill
rate (:func:`oom_rate_outcome`, :func:`increase_reliability`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from greenkube.impact.types import CO2E_COMPONENTS, MEASURED_CO2E_COMPONENTS
from greenkube.impact.types import HOURS_PER_YEAR as IMPACT_HOURS_PER_YEAR
from greenkube.models.verification import HealthGateResult, HealthGateStatus
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
    hour_of_week,
)
from greenkube.verify.regression import METHOD_OLS, OlsCounterfactual, residual_bootstrap
from greenkube.verify.series import (
    carbon_factor,
    co2e_series,
    has_embodied,
    hourly_carbon_factors,
    missing_reason,
    quantity_series,
    window_hours,
)
from greenkube.verify.statistics import (
    INTERVAL_METHOD,
    BetweenWeekTerm,
    between_week_term,
    circular_block_bootstrap,
    outcome_rule,
    seed_for,
    with_between_week,
)

HOURS_PER_YEAR = float(IMPACT_HOURS_PER_YEAR)  # one annualisation constant with the Impact Model (MEA-13)
#: The hour-of-week MVP counterfactual (C3), the fallback of the regression.
MVP_METHOD = "hour_of_week_mean"
#: Projection key of the CO2e of every component, embodied included (component matching).
CO2E_WITH_EMBODIED = "co2e_g_with_embodied"
#: Fewest complete days of aligned post-change hours a decided verdict needs (MEA-01).
MIN_POST_DAYS = 5

# Accelerated demo windows (CHANGE_MEASUREMENT_ACCELERATED, quickstart only, MEA-10). These are
# demo thresholds, never used outside the accelerated mode: a 30-minute post-window decides, so
# the verdict shows the pipeline end to end, not a representative saving.
#: Time grid of accelerated measurements: 5-minute slots instead of UTC clock hours.
ACCELERATED_SLOT_SECONDS = 300
#: Counterfactual of accelerated measurements: the baseline mean (no hour-of-week seasonality).
ACCELERATED_MODEL = "flat_mean_accelerated"
#: Fewest aligned post-window slots an accelerated verdict needs (30 minutes).
ACCELERATED_MIN_POST_SLOTS = 6
#: Block length (slots) of the accelerated bootstraps (15 minutes).
ACCELERATED_BLOCK_SLOTS = 3
#: ``counterfactual_params.interval_method`` of accelerated measurements.
ACCELERATED_INTERVAL_METHOD = "circular_block_bootstrap_t_with_baseline_mean"


@dataclass(frozen=True)
class MeasurementSettings:
    """Tunables of a change measurement (``CHANGE_MEASUREMENT_*`` settings)."""

    pre_window_days: float = 7.0
    post_window_days: float = 7.0
    warmup_hours: float = 1.0
    min_coverage: float = 0.8
    bootstrap_samples: int = 1000
    epsilon_ratio: float = 0.02
    #: Week-to-week coefficient of variation assumed with fewer than two baseline weeks (MEA-01).
    prior_week_cv: float = 0.05
    #: Accelerated demo windows (MEA-10): 5-minute slots, flat counterfactual, demo thresholds.
    accelerated: bool = False

    @property
    def slot_seconds(self) -> int:
        """Length of a bucket: one clock hour, or :data:`ACCELERATED_SLOT_SECONDS` when accelerated."""
        return ACCELERATED_SLOT_SECONDS if self.accelerated else 3600

    @classmethod
    def from_config(cls, config: Any) -> "MeasurementSettings":
        return cls(
            pre_window_days=float(getattr(config, "CHANGE_MEASUREMENT_PRE_WINDOW_DAYS", 7.0)),
            post_window_days=float(getattr(config, "CHANGE_MEASUREMENT_POST_WINDOW_DAYS", 7.0)),
            warmup_hours=float(getattr(config, "CHANGE_MEASUREMENT_WARMUP_HOURS", 1.0)),
            min_coverage=float(getattr(config, "CHANGE_MEASUREMENT_MIN_COVERAGE", 0.8)),
            bootstrap_samples=int(getattr(config, "CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES", 1000)),
            epsilon_ratio=float(getattr(config, "CHANGE_MEASUREMENT_EPSILON_RATIO", 0.02)),
            prior_week_cv=float(getattr(config, "CHANGE_MEASUREMENT_PRIOR_WEEK_CV", 0.05)),
            accelerated=getattr(config, "CHANGE_MEASUREMENT_ACCELERATED", False) is True,
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
    #: The version recorded on the measurement when it differs from the strategy's (e.g. the rate provenance).
    strategy_version: Optional[str] = None


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
        rate_sources: Sequence[str] = (),
        price_card: Optional[Mapping[str, Any]] = None,
    ) -> StrategyResult: ...


class RequestRightsizingStrategy:
    """Measures a change of container requests (C3 MVP).

    Quantities, all per hour and signed (positive = saving):

    * ``allocated_cost`` — trusted allocated cost of the workload at the
      baseline unit prices (claimed; decides the outcome, see 2.4 below);
    * ``allocated_cost_observed`` — the same cost at the prices of each window,
      as stored (reported; its difference from ``allocated_cost`` is the price
      effect);
    * ``cost_cpu`` / ``cost_ram`` — its CPU and RAM components at the baseline
      unit prices (used to split the saving between the change's
      recommendations, C5);
    * ``core_hours`` / ``gib_hours`` — requested capacity freed (reported);
    * ``co2e_g`` — CO2e of the workload as the Impact Model allocates it
      (idle power by CPU reservation, memory power by memory reservation,
      dynamic and GPU power by usage, embodied emissions by reservation), so
      request changes move it. Embodied emissions are included only when the
      baseline snapshot carries them (snapshots frozen before they were
      measured do not); ``components`` lists what was measured, and the
      projection it is compared with covers the same components;
    * ``embodied_co2e_g`` — the embodied part alone (reported);
    * ``energy_kwh`` — energy of the workload (reported; the basis of the
      CO2e saving since 2.4).

    Each quantity's counterfactual is the load regression (C3 v2,
    :mod:`greenkube.verify.regression`) when a load driver exists and the fit's
    diagnostics pass, otherwise the hour-of-week MVP; ``method`` and the
    diagnostics are recorded.

    **Per replica (2.1).** A request change moves what each replica is
    charged, not how many replicas run. Every quantity is measured *per
    replica* (each hour's value over its mean replica count), and the per-replica
    saving is multiplied by the replicas that actually ran in the post-window:
    ``saving = Δ(per-replica rate) × post replica-hours``, the roadmap's
    ``Σ Δrequest × replica-hours``. A replica count that drops for another
    reason (4 → 2 during the post-window) is therefore never credited to the
    change; it is reported by the ``replica_shift`` confounder. Hours without
    a replica count are left out. Measurements decided before 2.1 compared
    workload totals.

    **Both bases, the smaller claim (2.2).** Per-replica scaling alone
    over-credits the mirror case: a CPU-request cut that makes an HPA scale
    out (2 pods at $0.01 -> 4 pods at $0.005) halves the per-pod cost while the
    workload total is unchanged. Every quantity is therefore also fitted on
    workload totals, and the estimate with the smaller mean saving is kept
    (``counterfactual_params.replica_basis``). It stays ``realized`` only
    when both bases are realized; otherwise it is ``inconclusive``. Neither a
    replica drop nor a replica rise is credited to the change.

    **Valid verdicts (2.3, MEA-01/MEA-09).** The interval adds a between-week
    term to the circular block bootstrap (:mod:`greenkube.verify.statistics`),
    ``realized`` needs its lower bound above the noise floor, and a decided
    verdict needs at least ``MIN_POST_DAYS`` days of aligned post-change hours.
    Measurements decided before 2.3 used a percentile moving-block bootstrap
    without the between-week term and ``realized`` above zero.

    **Constant prices (2.4, MEA-02).** A price move between the windows (a
    spot price, a reserved-instance or custom-pricing edit) is not a saving of
    the change. Each hour's CPU and RAM cost is revalued as its *charged*
    quantity, ``max(request, usage)`` per sample as OpenCost charges it, times
    the baseline unit price per charged unit of the frozen snapshot
    ``price_card`` (snapshots frozen before charged quantities were measured:
    ``max(request, mean usage)`` derived per hour,
    ``constant_price_basis = charged_derived``); the rest of the cost (GPU, storage)
    is kept as observed. The observed delta is reported as
    ``allocated_cost_observed`` and ``price_effect = observed - constant`` is
    recorded; the ``price_card_change`` confounder reports the move. Without
    baseline prices the observed cost is measured (``constant_price_basis =
    observed``). Measurements decided before 2.4 used the observed cost.

    **CO2e at the post-window carbon factor (2.4, MEA-03).** The operational
    CO2e of every hour of both windows is its energy times a post-window
    carbon factor (grid intensity x PUE, :class:`PostCarbonFactors`): each
    post hour at its own factor, each baseline hour at the post factor of the
    same hour of week (time-averaged post factor as fallback), so the claimed CO2e saving
    is each hour's energy saving valued at what the grid emitted per kWh in
    that hour after the change, plus the embodied delta; a windier or calmer
    post week is reported by the ``intensity_shift`` confounder, not booked. Without a post
    carbon factor (no energy) the raw difference is measured
    (``co2e_basis = raw_delta``).

    **Template changes (2.5, MEA-08).** A pod-template change during the
    measurement with the change's requests and limits (same resources digest:
    an image deployment, an env change, a rollout restart) is a mild or
    moderate ``template_change`` confounder instead of a severe
    ``spec_change``, so it no longer contaminates the outcome.

    **Accelerated demo windows (2.5, MEA-10).** With
    ``MeasurementSettings.accelerated`` the buckets are 5-minute slots, the
    counterfactual is the baseline mean (:func:`_fit_flat`) and a verdict
    needs :data:`ACCELERATED_MIN_POST_SLOTS` slots; the result records
    ``representative: false``. The reason of a quantity without a value names
    the true cause (:func:`~greenkube.verify.series.missing_reason`).
    """

    key = "request_rightsizing"
    version = "2.6"
    primary = "allocated_cost"

    QUANTITIES = (
        QuantitySpec("allocated_cost", "currency", True, "allocated cost"),
        QuantitySpec("allocated_cost_observed", "currency", False, "allocated cost at observed prices"),
        QuantitySpec("cost_cpu", "currency", False, "allocated CPU cost"),
        QuantitySpec("cost_ram", "currency", False, "allocated RAM cost"),
        QuantitySpec("core_hours", "core-hours", False, "requested CPU cores"),
        QuantitySpec("gib_hours", "GiB-hours", False, "requested memory"),
        QuantitySpec("co2e_g", "gCO2e", True, "allocated CO2e"),
        QuantitySpec("embodied_co2e_g", "gCO2e", False, "embodied CO2e"),
        QuantitySpec("energy_kwh", "kWh", False, "energy"),
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
        rate_sources: Sequence[str] = (),
        price_card: Optional[Mapping[str, Any]] = None,
    ) -> StrategyResult:
        """Estimate every quantity of :attr:`QUANTITIES` and the change outcome.

        ``price_card`` is the frozen baseline snapshot's price card (the unit
        prices the request effect is valued at, MEA-02); without one, the
        same prices are computed from ``pre``.
        """
        del rate_sources  # measured from the workload's own trusted rows
        constant = constant_price_windows(pre, post, price_card)
        expected_hours = len(window_hours(plan.post, settings.slot_seconds)) or 1
        estimates: List[SavingsEstimate] = []
        methods: Dict[str, str] = {}
        elasticities: Dict[str, float] = {}
        matched: Dict[str, int] = {}
        diagnostics: Dict[str, Any] = {}
        bases: Dict[str, str] = {}
        intervals: Dict[str, Dict[str, Any]] = {}
        embodied = has_embodied(pre) and has_embodied(post)
        factor = carbon_factor(post)
        post_factors = PostCarbonFactors.of(post)
        for spec in self.QUANTITIES:
            observed = spec.quantity == "allocated_cost_observed"
            fitted = self._estimate_quantity(
                spec,
                pre if observed else constant.pre,
                post if observed else constant.post,
                load,
                expected_hours,
                projections,
                settings,
                seed_parts,
                embodied=embodied,
                baseline_end=ensure_utc(plan.pre.end),
                co2e_factors=post_factors,
            )
            estimates.append(fitted.estimate)
            bases[spec.quantity] = fitted.basis
            if fitted.interval_params:
                intervals[spec.quantity] = fitted.interval_params
            if fitted.mvp is not None:
                elasticities[spec.quantity] = round(fitted.mvp.elasticity, 6)
                matched[spec.quantity] = len(fitted.mvp.means)
            if fitted.estimate.method is not None:
                methods[spec.quantity] = fitted.estimate.method
            if fitted.diagnostics is not None:
                diagnostics[spec.quantity] = fitted.diagnostics
        params: Dict[str, Any] = {
            "model": ACCELERATED_MODEL if settings.accelerated else methods.get(self.primary, MVP_METHOD),
            "methods": methods,
            "elasticity": elasticities,
            "matched_hours_of_week": matched,
            "diagnostics": diagnostics,
            "replica_basis": bases,
            "co2e_components": list(_co2e_components(embodied)),
            "block_hours": 24,
            "bootstrap_samples": settings.bootstrap_samples,
            "epsilon_ratio": settings.epsilon_ratio,
            "interval_method": INTERVAL_METHOD,
            "prior_week_cv": settings.prior_week_cv,
            "baseline_weeks": {key: value["baseline_weeks"] for key, value in intervals.items()},
            "between_week_cv": {key: value["between_week_cv"] for key, value in intervals.items()},
            "between_week_basis": {key: value["between_week_basis"] for key, value in intervals.items()},
            **constant.as_params(),
            "co2e_basis": CO2E_BASIS_ENERGY if factor is not None else CO2E_BASIS_RAW,
            "co2e_factor_pre": _rounded(carbon_factor(pre)),
            "co2e_factor_post": _rounded(factor),
            "co2e_factor_fallback": _rounded(post_factors.fallback) if post_factors is not None else None,
            "co2e_factor_fallback_share": _rounded(post_factors.fallback_share(pre))
            if post_factors is not None
            else None,
            "co2e_raw_delta": _raw_co2e_delta(pre, post, embodied),
        }
        if settings.accelerated:
            # Demo windows: decided on demo thresholds, not representative of a saving (MEA-10).
            params.update(
                representative=False,
                time_grid_seconds=ACCELERATED_SLOT_SECONDS,
                interval_method=ACCELERATED_INTERVAL_METHOD,
                block_hours=ACCELERATED_BLOCK_SLOTS * ACCELERATED_SLOT_SECONDS / 3600,
                demo_thresholds={
                    "min_post_slots": ACCELERATED_MIN_POST_SLOTS,
                    "slot_seconds": ACCELERATED_SLOT_SECONDS,
                    "block_slots": ACCELERATED_BLOCK_SLOTS,
                },
            )
        claimed_rate = next(item for item in estimates if item.quantity == self.primary).rate_per_hour
        observed_rate = next(item for item in estimates if item.quantity == "allocated_cost_observed").rate_per_hour
        params["price_effect"] = (
            observed_rate - claimed_rate if observed_rate is not None and claimed_rate is not None else None
        )

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
        baseline_end: Optional[datetime] = None,
        co2e_factors: Optional[PostCarbonFactors] = None,
    ) -> "_Fitted":
        components: List[str] = []
        projected = projections.get(spec.quantity)
        if spec.quantity == "co2e_g":
            # Component matching: the measured CO2e covers embodied emissions only when both
            # windows carry them (snapshots frozen before they were measured do not).
            components = list(_co2e_components(embodied))
            pre_values = _co2e_at_factor(pre, embodied, co2e_factors, post=False)
            post_values = _co2e_at_factor(post, embodied, co2e_factors, post=True)
            projected = projections.get(CO2E_WITH_EMBODIED) if embodied else projections.get("co2e_g")
        else:
            pre_values = quantity_series(pre, spec.quantity)
            post_values = quantity_series(post, spec.quantity)
        post_replicas = quantity_series(post, "replicas")
        unit = "slot" if settings.accelerated else "hour"
        diagnosed = "co2e_g" if spec.quantity == "co2e_g" else spec.quantity

        def missing(window: str) -> str:
            buckets = pre if window == "baseline" else post
            return missing_reason(buckets, diagnosed, spec.description, window, unit=unit)

        base: Dict[str, Any] = {
            "quantity": spec.quantity,
            "unit": spec.unit,
            "claimed": spec.claimed,
            "projected_annual": projected,
            "components": components,
        }
        fit = partial(
            RequestRightsizingStrategy._fit_basis,
            spec,
            base=base,
            load=load,
            expected_hours=expected_hours,
            projected=projected,
            settings=settings,
            seed_parts=seed_parts,
            baseline_end=baseline_end,
            missing=missing,
        )
        per_replica = fit(
            _per_replica(pre_values, quantity_series(pre, "replicas")),
            _per_replica(post_values, post_replicas),
            post_replicas,
        )
        if per_replica.estimate.rate_per_hour is None:
            return per_replica
        totals = fit(pre_values, post_values, None)
        totals.basis = BASIS_WORKLOAD_TOTAL
        if totals.estimate.rate_per_hour is None:
            return per_replica
        return _conservative(per_replica, totals)

    @staticmethod
    def _fit_basis(
        spec: QuantitySpec,
        pre_values: Mapping[datetime, float],
        post_values: Mapping[datetime, float],
        post_replicas: Optional[Mapping[datetime, float]],
        *,
        base: Mapping[str, Any],
        load: Optional[ResolvedLoad],
        expected_hours: int,
        projected: Optional[float],
        settings: MeasurementSettings,
        seed_parts: Sequence[object],
        baseline_end: Optional[datetime] = None,
        missing: Optional[Callable[[str], str]] = None,
    ) -> "_Fitted":
        """Fit one basis: per-replica values scaled by ``post_replicas``, or workload totals (``None``).

        ``baseline_end`` (default: one hour after the last baseline hour) anchors
        the baseline weeks of the between-week term. ``missing(window)`` names
        why a window has no value (MEA-10: no rows, untrusted cost, hours below
        the coverage threshold). Accelerated settings fit the flat demo model
        (:func:`_fit_flat`).
        """
        if not pre_values or not post_values:
            window = "baseline" if not pre_values else "post-change"
            return _Fitted(
                SavingsEstimate(
                    **base,
                    outcome=ChangeOutcome.INCONCLUSIVE,
                    reason=missing(window) if missing is not None else f"no {spec.description} in the {window} window",
                )
            )
        if settings.accelerated:
            return _fit_flat(
                spec,
                pre_values,
                post_values,
                post_replicas,
                base=base,
                expected_hours=expected_hours,
                projected=projected,
                settings=settings,
                seed=seed_for(*seed_parts, spec.quantity),
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
        residuals: Dict[datetime, float] = {}
        if ols is not None and load is not None:
            method = METHOD_OLS
            residuals = {hour: residual for hour, _, _, residual in ols.baseline}
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
            interval = circular_block_bootstrap(
                [item.saving for item in aligned], seed=seed, samples=settings.bootstrap_samples
            )
            elasticity = mvp.elasticity
            for hour, value in pre_values.items():
                fitted_value = mvp.predict(hour, load.pre.get(hour) if load is not None else None)
                if fitted_value is not None:
                    residuals[hour] = value - fitted_value
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
        # Week-level shifts are constant offsets of every hourly saving: add their variance (MEA-01).
        term = between_week_term(
            residuals,
            list(pre_values.values()),
            baseline_end if baseline_end is not None else max(pre_values) + timedelta(hours=1),
            counterfactual=math.fsum(item.counterfactual for item in aligned) / len(aligned),
            post_hours=len(aligned),
            prior_cv=settings.prior_week_cv,
        )
        interval = with_between_week(interval, term)
        interval_params = {"interval_method": INTERVAL_METHOD, **term.as_params()}
        # Per-replica rates times the replicas that ran: the saving of the post-window's replica-hours.
        replicas = (
            math.fsum(post_replicas[item.hour] for item in aligned) / len(aligned) if post_replicas is not None else 1.0
        )
        interval = interval.scaled(replicas)
        counterfactual = math.fsum(item.counterfactual for item in aligned) / len(aligned) * replicas
        actual = math.fsum(item.actual for item in aligned) / len(aligned) * replicas
        epsilon = settings.epsilon_ratio * abs(counterfactual)
        if coverage < settings.min_coverage:
            outcome = ChangeOutcome.INCONCLUSIVE
            reason = f"aligned coverage {coverage:.0%} of the post-window is below {settings.min_coverage:.0%}"
        elif len(aligned) < MIN_POST_DAYS * 24:
            outcome = ChangeOutcome.INCONCLUSIVE
            reason = (
                f"{len(aligned)} aligned post-change hours of {spec.description}: a verdict needs at least "
                f"{MIN_POST_DAYS} complete days"
            )
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
            interval_params=interval_params,
        )


def _fit_flat(
    spec: QuantitySpec,
    pre_values: Mapping[datetime, float],
    post_values: Mapping[datetime, float],
    post_replicas: Optional[Mapping[datetime, float]],
    *,
    base: Mapping[str, Any],
    expected_hours: int,
    projected: Optional[float],
    settings: MeasurementSettings,
    seed: int,
) -> "_Fitted":
    """Accelerated demo fit (MEA-10): the counterfactual of every post slot is the baseline mean.

    The saving of each slot is ``mean(baseline) - post(slot)``. Its interval
    adds the standard error of the post-slot savings and of the baseline mean,
    each from a circular block bootstrap with blocks of
    :data:`ACCELERATED_BLOCK_SLOTS` slots (Welch-Satterthwaite degrees of
    freedom). A verdict needs :data:`ACCELERATED_MIN_POST_SLOTS` aligned slots
    and the usual minimum coverage and noise floor. No seasonality, load
    driver or between-week term: the result is labelled
    ``representative: false`` by the strategy.
    """
    baseline = [pre_values[hour] for hour in sorted(pre_values)]
    level = math.fsum(baseline) / len(baseline)
    hours = sorted(post_values)
    savings = [level - post_values[hour] for hour in hours]
    interval = circular_block_bootstrap(
        savings, seed=seed, samples=settings.bootstrap_samples, block=ACCELERATED_BLOCK_SLOTS
    )
    level_interval = circular_block_bootstrap(
        baseline, seed=seed + 1, samples=settings.bootstrap_samples, block=ACCELERATED_BLOCK_SLOTS
    )
    level_se = level_interval.se if level_interval.se is not None else 0.0
    term = BetweenWeekTerm(
        se=level_se,
        dof=level_interval.dof if level_interval.dof is not None else math.inf,
        baseline_weeks=0,
        post_weeks=0.0,
        basis="baseline_mean_accelerated",
        cv=None,
    )
    interval = with_between_week(interval, term)
    replicas = math.fsum(post_replicas[hour] for hour in hours) / len(hours) if post_replicas is not None else 1.0
    interval = interval.scaled(replicas)
    counterfactual = level * replicas
    actual = math.fsum(post_values[hour] for hour in hours) / len(hours) * replicas
    coverage = len(hours) / expected_hours
    epsilon = settings.epsilon_ratio * abs(counterfactual)
    if coverage < settings.min_coverage:
        outcome = ChangeOutcome.INCONCLUSIVE
        reason = f"aligned coverage {coverage:.0%} of the post-window is below {settings.min_coverage:.0%}"
    elif len(hours) < ACCELERATED_MIN_POST_SLOTS:
        outcome = ChangeOutcome.INCONCLUSIVE
        reason = (
            f"{len(hours)} post-change slots of {spec.description}: an accelerated (demo) verdict needs at least "
            f"{ACCELERATED_MIN_POST_SLOTS} slots of {ACCELERATED_SLOT_SECONDS // 60} minutes"
        )
    else:
        outcome = outcome_rule(interval.low, interval.high, epsilon)
        reason = _reason(spec, outcome, interval.low, interval.high, epsilon) + " (accelerated demo windows)"
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
            aligned_hours=len(hours),
            elasticity=0.0,
            method=ACCELERATED_MODEL,
            outcome=outcome,
            reason=reason,
            projection_accuracy=(annualized / projected) if projected else None,
        ),
        interval_params={"interval_method": ACCELERATED_INTERVAL_METHOD, **term.as_params()},
    )


#: Each post hour at its own carbon factor, each baseline hour at the post factor of its hour of week.
CO2E_BASIS_ENERGY = "energy_delta_at_post_hourly_intensity"
CO2E_BASIS_RAW = "raw_delta"


def _rounded(value: Optional[float]) -> Optional[float]:
    return round(value, 6) if value is not None else None


@dataclass(frozen=True)
class PostCarbonFactors:
    """Post-window carbon factors the CO2e of both windows is valued at (MEA-03).

    ``by_hour`` is each trusted post hour's own factor (the bucket's grid
    intensity x PUE, else ``co2e / energy``), ``by_hour_of_week`` the time
    average of the post factors of each hour of the week, ``fallback`` the time
    average of all post hours' factors (for an hour of the week without a
    trusted post hour; not energy-weighted, so it carries no load weighting)
    and ``window`` the energy-weighted factor of the whole post-window (the
    fallback only when no hour has its own factor, and the reported
    ``co2e_factor_post``).
    """

    window: float
    by_hour: Dict[datetime, float]
    by_hour_of_week: Dict[int, float]
    fallback: float

    @classmethod
    def of(cls, post: Sequence[HourBucket]) -> Optional["PostCarbonFactors"]:
        """The factors of ``post``, or ``None`` without a post-window factor."""
        window = carbon_factor(post)
        if window is None:
            return None
        by_hour = hourly_carbon_factors(post)
        groups: Dict[int, List[float]] = {}
        for hour, factor in by_hour.items():
            groups.setdefault(hour_of_week(hour), []).append(factor)
        by_week = {key: math.fsum(values) / len(values) for key, values in groups.items()}
        fallback = math.fsum(by_hour.values()) / len(by_hour) if by_hour else window
        return cls(window=window, by_hour=by_hour, by_hour_of_week=by_week, fallback=fallback)

    def baseline_factor(self, hour: datetime) -> float:
        """The post factor of ``hour``'s hour of week, else the time-averaged post factor."""
        return self.by_hour_of_week.get(hour_of_week(hour), self.fallback)

    def post_factor(self, hour: datetime) -> float:
        """The post hour's own factor, else that of its hour of week, else the time-averaged post factor."""
        return self.by_hour.get(hour, self.baseline_factor(hour))

    def fallback_share(self, pre: Sequence[HourBucket]) -> Optional[float]:
        """Share of the baseline energy valued at the fallback factor (no post factor at its hour of week)."""
        energy = quantity_series(pre, "energy_kwh")
        total = math.fsum(energy.values())
        if total <= 0:
            return None
        missing = math.fsum(value for hour, value in energy.items() if hour_of_week(hour) not in self.by_hour_of_week)
        return missing / total


def _co2e_at_factor(
    buckets: Sequence[HourBucket], embodied: bool, factors: Optional[PostCarbonFactors], *, post: bool
) -> Dict[datetime, float]:
    """CO2e per hour: energy x the post-window carbon factor of that hour (MEA-03), plus embodied CO2e.

    A post hour is valued at its own factor, a baseline hour at the post
    factor of the same hour of week (time-averaged post factor as fallback), so the
    claimed saving is ``sum((E_pre - E_post) x factor_post)`` hour by hour and a
    load that follows the grid intensity does not bias it. Only hours with
    trusted carbon count (as before), so an install without measured intensity
    still has no CO2e verdict. Without factors the stored CO2e is used
    (``raw_delta``).
    """
    stored = co2e_series(buckets, embodied=embodied)
    if factors is None:
        return stored
    value_at = factors.post_factor if post else factors.baseline_factor
    energy = quantity_series(buckets, "energy_kwh")
    manufactured = quantity_series(buckets, "embodied_co2e_g") if embodied else {}
    return {
        hour: energy[hour] * value_at(hour) + (manufactured.get(hour, 0.0) if embodied else 0.0)
        for hour in stored
        if hour in energy
    }


def _raw_co2e_delta(pre: Sequence[HourBucket], post: Sequence[HourBucket], embodied: bool) -> Optional[float]:
    """Mean stored CO2e per hour before minus after (the pre-2.4 basis, reported next to the claim)."""
    before = co2e_series(pre, embodied=embodied)
    after = co2e_series(post, embodied=embodied)
    if not before or not after:
        return None
    return round(math.fsum(before.values()) / len(before) - math.fsum(after.values()) / len(after), 9)


CONSTANT_PRICE_CHARGED = "charged"
#: Charged quantities derived for the hours without them: ``max(core_hours, cpu_usage_cores)`` and ``gib_hours``.
CONSTANT_PRICE_DERIVED = "charged_derived"
CONSTANT_PRICE_REQUESTED = "requested"
CONSTANT_PRICE_OBSERVED = "observed"


@dataclass(frozen=True)
class ConstantPriceWindows:
    """Both windows with CPU and RAM cost revalued at the baseline unit prices (MEA-02)."""

    pre: Sequence[HourBucket]
    post: Sequence[HourBucket]
    #: ``charged`` (max(request, usage) per sample), ``charged_derived`` (older snapshots: max(request,
    #: mean usage) per hour), ``requested`` (no usage to derive it from) or ``observed`` (no prices).
    basis: str
    prices: Dict[str, float]
    #: ``snapshot_price_card`` or ``baseline_hours`` (computed from the baseline buckets).
    source: Optional[str]

    def as_params(self) -> Dict[str, Any]:
        """Values recorded in ``counterfactual_params``."""
        return {
            "constant_price_basis": self.basis,
            "constant_prices": {key: round(value, 10) for key, value in self.prices.items()},
            "constant_price_source": self.source,
        }


_CARD_KEYS = {
    CONSTANT_PRICE_CHARGED: {"cpu": "cpu_charged_core_hour", "memory": "memory_charged_gib_hour"},
    CONSTANT_PRICE_REQUESTED: {"cpu": "cpu_core_hour", "memory": "memory_gib_hour"},
}


def _card_prices(card: Optional[Mapping[str, Any]], basis: str) -> Optional[Dict[str, float]]:
    if not card:
        return None
    prices: Dict[str, float] = {}
    for resource, key in _CARD_KEYS[basis].items():
        value = card.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            return None
        prices[resource] = float(value)
    return prices


def _with_derived_charge(buckets: Sequence[HourBucket]) -> tuple[List[HourBucket], bool]:
    """``buckets`` with the charged quantities derived where missing, and whether all costed hours have them.

    An hour without ``charged_core_hours`` gets ``max(core_hours, cpu_usage_cores)``
    (the hourly mean usage: a lower bound of the per-sample maximum OpenCost
    charges) and ``charged_gib_hours = gib_hours`` (memory usage is not in the
    buckets). An hour without CPU usage keeps no charged quantity.
    """
    out: List[HourBucket] = []
    for bucket in buckets:
        if bucket.charged_core_hours is not None and bucket.charged_gib_hours is not None:
            out.append(bucket)
            continue
        if bucket.core_hours is None or bucket.cpu_usage_cores is None or bucket.gib_hours is None:
            out.append(bucket)
            continue
        out.append(
            bucket.model_copy(
                update={
                    "charged_core_hours": max(bucket.core_hours, bucket.cpu_usage_cores),
                    "charged_gib_hours": bucket.gib_hours,
                }
            )
        )
    from greenkube.verify.series import has_charged

    return out, has_charged(out)


def constant_price_windows(
    pre: Sequence[HourBucket], post: Sequence[HourBucket], card: Optional[Mapping[str, Any]]
) -> ConstantPriceWindows:
    """Revalue the CPU and RAM cost of every hour at the baseline unit prices.

    The charged basis needs the charged quantities in both windows and the
    charged prices (from ``card``, else from ``pre``). Windows from snapshots
    frozen before charged quantities were measured get them derived from the
    request and the hourly CPU usage (``charged_derived``, review wp07-2: a
    request cut below the usage otherwise books a saving the allocator never
    gives), priced from ``card``'s charged prices, else from the derived
    baseline hours, else at ``card``'s per-request prices. Without usage to
    derive from, the requested basis. An hour without cost components or
    quantities loses its cost (it is not measured, which lowers the coverage).
    Without any baseline price the windows are returned unchanged with
    ``basis = observed`` (the ``constant_prices_unavailable`` confounder).
    """
    from greenkube.verify.series import PRICED_RESOURCES, has_charged, unit_prices

    prices: Optional[Dict[str, float]] = None
    source: Optional[str] = "snapshot_price_card"
    if has_charged(pre) and has_charged(post):
        basis = CONSTANT_PRICE_CHARGED
        prices = _card_prices(card, CONSTANT_PRICE_CHARGED)
    else:
        derived_pre, pre_ok = _with_derived_charge(pre)
        derived_post, post_ok = _with_derived_charge(post)
        if pre_ok and post_ok:
            basis = CONSTANT_PRICE_DERIVED
            pre, post = derived_pre, derived_post
            prices = _card_prices(card, CONSTANT_PRICE_CHARGED)
        else:
            basis = CONSTANT_PRICE_REQUESTED
            prices = _card_prices(card, CONSTANT_PRICE_REQUESTED)
    if prices is None:
        computed = unit_prices(pre, charged=basis != CONSTANT_PRICE_REQUESTED)
        if all(value is not None for value in computed.values()):
            prices = {key: float(value) for key, value in computed.items() if value is not None}
            source = "baseline_hours"
    if prices is None and basis == CONSTANT_PRICE_DERIVED:
        prices = _card_prices(card, CONSTANT_PRICE_REQUESTED)
        source = "snapshot_price_card_requested"
    if prices is None:
        return ConstantPriceWindows(pre=pre, post=post, basis=CONSTANT_PRICE_OBSERVED, prices={}, source=None)
    quantity_index = 2 if basis == CONSTANT_PRICE_REQUESTED else 1

    def revalue(buckets: Sequence[HourBucket]) -> List[HourBucket]:
        out: List[HourBucket] = []
        for bucket in buckets:
            if bucket.cost is None:
                out.append(bucket)
                continue
            components: Dict[str, float] = {}
            for resource, fields in PRICED_RESOURCES.items():
                amount = getattr(bucket, fields[quantity_index])
                if getattr(bucket, fields[0]) is None or amount is None:
                    break
                components[fields[0]] = float(amount) * prices[resource]
            if len(components) != len(PRICED_RESOURCES):
                out.append(bucket.model_copy(update={"cost": None, "cost_cpu": None, "cost_ram": None}))
                continue
            # GPU, storage and any other cost component is not moved by a request change: kept as observed.
            other = float(bucket.cost) - float(bucket.cost_cpu or 0.0) - float(bucket.cost_ram or 0.0)
            out.append(bucket.model_copy(update={**components, "cost": math.fsum(components.values()) + other}))
        return out

    return ConstantPriceWindows(pre=revalue(pre), post=revalue(post), basis=basis, prices=prices, source=source)


def _per_replica(values: Mapping[datetime, float], replicas: Mapping[datetime, float]) -> Dict[datetime, float]:
    """``{hour: value / replicas}`` over the hours with a positive replica count."""
    return {
        hour: value / replicas[hour]
        for hour, value in values.items()
        if replicas.get(hour) is not None and replicas[hour] > 0 and math.isfinite(replicas[hour])
    }


BASIS_PER_REPLICA = "per_replica"
BASIS_WORKLOAD_TOTAL = "workload_total"


@dataclass
class _Fitted:
    estimate: SavingsEstimate
    mvp: Optional[HourOfWeekCounterfactual] = None
    diagnostics: Optional[Dict[str, Any]] = None
    basis: str = BASIS_PER_REPLICA
    #: Interval method, baseline weeks and between-week variation (``counterfactual_params``).
    interval_params: Optional[Dict[str, Any]] = None


def _conservative(per_replica: _Fitted, totals: _Fitted) -> _Fitted:
    """The smaller of the per-replica and workload-total savings (fail closed in both directions).

    Per-replica scaling stops an unrelated replica drop from being credited; the
    workload total stops a replica rise the change itself caused (an HPA scaling
    out after a CPU-request cut: per-pod cost halved, twice the pods) from being
    credited. The claim is the smaller mean saving, and it stays ``realized``
    only when both bases are realized.
    """
    per_rate = per_replica.estimate.rate_per_hour or 0.0
    total_rate = totals.estimate.rate_per_hour or 0.0
    chosen, other = (per_replica, totals) if per_rate <= total_rate else (totals, per_replica)
    estimate = chosen.estimate
    if estimate.outcome is ChangeOutcome.REALIZED and other.estimate.outcome is not ChangeOutcome.REALIZED:
        chosen.estimate = estimate.model_copy(
            update={
                "outcome": ChangeOutcome.INCONCLUSIVE,
                "reason": (
                    f"{estimate.reason}; the {other.basis.replace('_', ' ')} measurement is "
                    f"{other.estimate.outcome.value}, so the saving is not attributed to the change"
                ),
            }
        )
    return chosen


def _co2e_components(embodied: bool) -> tuple[str, ...]:
    return CO2E_COMPONENTS if embodied else MEASURED_CO2E_COMPONENTS


def _reason(
    spec: QuantitySpec, outcome: ChangeOutcome, low: Optional[float], high: Optional[float], epsilon: float
) -> str:
    if low is None or high is None:
        return f"{spec.description}: too few aligned hours for an interval"
    interval = f"90% CI [{low:+.6g}, {high:+.6g}] per hour"
    if outcome is ChangeOutcome.REALIZED:
        return f"{spec.description} saving realized: {interval} is above the noise floor {epsilon:.6g}"
    floor = f"the noise floor {epsilon:.6g}"
    if 0 < low <= epsilon:
        floor += " (a negligible saving: above zero but not above the floor)"
    if outcome is ChangeOutcome.NOT_REALIZED:
        return f"{spec.description} saving not realized: {interval} is at most {floor}"
    return f"{spec.description} saving inconclusive: {interval} straddles {floor}"


#: Strategies by the ``action_kind`` of the change (H0 registry keys).
STRATEGIES: Dict[str, SavingsMeasurementStrategy] = {"container_resources": RequestRightsizingStrategy()}
from greenkube.verify.deletion import ResourceDeletionStrategy  # noqa: E402  (registry line, H6)

STRATEGIES["delete_resource"] = ResourceDeletionStrategy()


def strategy_for(action_kind: str) -> Optional[SavingsMeasurementStrategy]:
    return STRATEGIES.get(action_kind)


# --------------------------------------------------------------------------- #
# Reliability outcome of request increases (H1)                                #
# --------------------------------------------------------------------------- #

#: One-sided significance of an OOM-rate drop (matches the 90% intervals of the savings estimates).
RELIABILITY_ALPHA = 0.10
#: Health gate replaced by the rate comparison for increases.
OOM_GATE = "oom_kills"
#: Restart gate: OOM restarts judged by the rate comparison are left out of it.
RESTART_GATE = "restarts"


@dataclass(frozen=True)
class ReliabilityOutcome:
    """OOM-kill rate before and after a request increase (H1): the outcome of a reliability fix.

    ``baseline_events`` are the OOM kills the recommendation observed over its
    evidence window (``evidence.oom_events``: container restarts whose termination
    was ``OOMKilled``, summed over pods); ``post_events`` the same count over the
    post-change window (``WorkloadHealthObservation.oom_restart_count``), never the
    number of pods that saw a kill. If the rate had not changed, the
    post-window share of all kills would be binomial with the post-window share of
    the observed time: a drop is ``realized`` when the post count is improbably low
    (one-sided p <= ``RELIABILITY_ALPHA``), ``not_realized`` when the rate did not
    drop, and ``inconclusive`` with too few kills or unknown counts.
    """

    metric: str
    outcome: ChangeOutcome
    reason: str
    baseline_events: Optional[int] = None
    baseline_hours: Optional[float] = None
    post_events: Optional[int] = None
    post_hours: Optional[float] = None
    p_value: Optional[float] = None
    #: Where ``baseline_hours`` comes from: ``workload_first_observed`` (from the workload's first
    #: observation in the evidence window) or ``evidence_window`` (fallback: the whole window).
    baseline_exposure_source: Optional[str] = None

    @property
    def baseline_rate_per_day(self) -> Optional[float]:
        return _per_day(self.baseline_events, self.baseline_hours)

    @property
    def post_rate_per_day(self) -> Optional[float]:
        return _per_day(self.post_events, self.post_hours)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "baseline_events": self.baseline_events,
            "baseline_hours": self.baseline_hours,
            "post_events": self.post_events,
            "post_hours": self.post_hours,
            "baseline_rate_per_day": self.baseline_rate_per_day,
            "post_rate_per_day": self.post_rate_per_day,
            "p_value": self.p_value,
            "baseline_exposure_source": self.baseline_exposure_source,
        }


def _per_day(events: Optional[int], hours: Optional[float]) -> Optional[float]:
    if events is None or hours is None or hours <= 0:
        return None
    return round(events / hours * 24.0, 6)


def _binomial_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact (n counts OOM kills, so it is small)."""
    if k >= n:
        return 1.0
    return math.fsum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1))


def oom_rate_outcome(
    baseline_events: Optional[int],
    baseline_seconds: Optional[float],
    post_events: Optional[int],
    post_seconds: Optional[float],
    *,
    alpha: float = RELIABILITY_ALPHA,
    baseline_exposure_source: Optional[str] = None,
) -> ReliabilityOutcome:
    """Did the OOM-kill rate drop after the increase? (exact conditional test, see :class:`ReliabilityOutcome`)."""
    baseline_hours = baseline_seconds / 3600.0 if baseline_seconds and baseline_seconds > 0 else None
    post_hours = post_seconds / 3600.0 if post_seconds and post_seconds > 0 else None
    base: Dict[str, Any] = {
        "metric": OOM_GATE,
        "baseline_events": baseline_events,
        "baseline_hours": round(baseline_hours, 4) if baseline_hours is not None else None,
        "post_events": post_events,
        "post_hours": round(post_hours, 4) if post_hours is not None else None,
        "baseline_exposure_source": baseline_exposure_source,
    }
    if baseline_events is None or baseline_hours is None:
        return ReliabilityOutcome(
            **base, outcome=ChangeOutcome.INCONCLUSIVE, reason="no OOM-kill count before the change"
        )
    if post_events is None or post_hours is None:
        return ReliabilityOutcome(
            **base, outcome=ChangeOutcome.INCONCLUSIVE, reason="no OOM-kill count after the change"
        )
    if baseline_events <= 0:
        return ReliabilityOutcome(
            **base,
            outcome=ChangeOutcome.INCONCLUSIVE,
            reason="no OOM kills before the change: the increase is judged by its usage headroom only",
        )
    baseline_rate = baseline_events / baseline_hours
    post_rate = post_events / post_hours
    if post_rate >= baseline_rate:
        return ReliabilityOutcome(
            **base,
            outcome=ChangeOutcome.NOT_REALIZED,
            reason=(
                f"OOM-kill rate did not drop: {post_rate * 24:.3g}/day after the change against "
                f"{baseline_rate * 24:.3g}/day before"
            ),
        )
    share = post_hours / (post_hours + baseline_hours)
    p_value = _binomial_cdf(post_events, post_events + baseline_events, share)
    if p_value <= alpha:
        outcome, verdict = ChangeOutcome.REALIZED, "dropped"
    else:
        outcome, verdict = ChangeOutcome.INCONCLUSIVE, "is lower, but too few kills were observed to tell"
    return ReliabilityOutcome(
        **base,
        outcome=outcome,
        p_value=round(p_value, 6),
        reason=(
            f"OOM-kill rate {verdict}: {post_rate * 24:.3g}/day after the change against "
            f"{baseline_rate * 24:.3g}/day before (one-sided p={p_value:.3g})"
        ),
    )


def _type_name(record: Any) -> Optional[str]:
    value = getattr(record, "type", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _raises(current: Any, proposed: Any) -> bool:
    numbers = (int, float)
    return (
        isinstance(current, numbers)
        and isinstance(proposed, numbers)
        and not isinstance(current, bool)
        and not isinstance(proposed, bool)
        and proposed > current
    )


#: Objective of a change (MEA-14): a saving, or a reliability fix (every member raises a request).
OBJECTIVE_SAVINGS = "savings"
OBJECTIVE_RELIABILITY = "reliability"


def change_objective(members: Sequence[Any]) -> str:
    """``reliability`` when every member raises a request (a reliability fix), else ``savings``.

    A mixed change (a decrease and an increase) keeps the savings objective.
    """
    items = list(members)
    return (
        OBJECTIVE_RELIABILITY if items and all(is_reliability_increase(item) for item in items) else OBJECTIVE_SAVINGS
    )


@dataclass(frozen=True)
class ReliabilityDecision:
    """The change outcome of a reliability fix (MEA-14): its reliability outcome, its cost reported apart."""

    outcome: ChangeOutcome
    reasons: List[str]
    params: Dict[str, Any]


def reliability_decision(
    members: Sequence[Any],
    cost_outcome: ChangeOutcome,
    estimates: Sequence[SavingsEstimate],
    reliability: Optional["ReliabilityOutcome"],
    gates: Sequence[HealthGateResult],
) -> Optional[ReliabilityDecision]:
    """Outcome of a change whose objective is reliability, ``None`` for a savings change (MEA-14).

    A request increase always costs more, so its cost outcome is never a
    saving: the change outcome is the reliability outcome instead (the OOM-kill
    rate drop, :func:`oom_rate_outcome`), ``realized`` only when the health
    gates pass too, and the cost is reported as ``cost_of_fix_annual``
    (positive: what the fix costs per year), never claimed as a saving. A
    contaminated measurement stays contaminated.
    """
    if change_objective(members) != OBJECTIVE_RELIABILITY:
        return None
    primary = next((item for item in estimates if item.quantity == "allocated_cost"), None)
    cost = -primary.annualized if primary is not None and primary.annualized is not None else None
    params: Dict[str, Any] = {"objective": OBJECTIVE_RELIABILITY, "cost_of_fix_annual": _rounded(cost)}
    cost_note = (
        f"cost of the fix {cost:+.2f}/yr (reported, not claimed as a saving)"
        if cost is not None
        else "cost of the fix not measured"
    )
    failed = [gate for gate in gates if gate.status is HealthGateStatus.FAILED]
    if cost_outcome is ChangeOutcome.CONTAMINATED:
        return ReliabilityDecision(ChangeOutcome.CONTAMINATED, [cost_note], params)
    if reliability is None:
        return ReliabilityDecision(
            ChangeOutcome.INCONCLUSIVE, ["reliability fix: no OOM-kill outcome was measured", cost_note], params
        )
    outcome = reliability.outcome
    if outcome is ChangeOutcome.REALIZED and failed:
        outcome = ChangeOutcome.INCONCLUSIVE
        reason = f"reliability fix: the OOM-kill rate dropped but {len(failed)} health gate(s) failed"
    elif outcome is ChangeOutcome.REALIZED:
        reason = f"reliability fix confirmed: {reliability.reason}"
    else:
        reason = f"reliability fix {outcome.value.replace('_', ' ')}: {reliability.reason}"
    return ReliabilityDecision(outcome, [reason, cost_note], params)


def is_reliability_increase(record: Any) -> bool:
    """A recommendation that raises a CPU or memory request (H1): a reliability fix.

    Same semantics as ``eligibility.proposes_increase``: when the record carries a
    type, only the resource of that type is compared.
    """
    rec_type = _type_name(record)
    cpu = _raises(
        getattr(record, "current_cpu_request_millicores", None),
        getattr(record, "recommended_cpu_request_millicores", None),
    )
    if rec_type == "RIGHTSIZING_CPU":
        return cpu
    if rec_type == "RIGHTSIZING_MEMORY":
        return raises_memory_request(record)
    return rec_type is None and (cpu or raises_memory_request(record))


def raises_memory_request(record: Any) -> bool:
    """A recommendation that raises the memory request: the only increase that can cure OOM kills."""
    rec_type = _type_name(record)
    if rec_type is not None and rec_type != "RIGHTSIZING_MEMORY":
        return False
    return _raises(
        getattr(record, "current_memory_request_bytes", None),
        getattr(record, "recommended_memory_request_bytes", None),
    )


def _lowers_memory_request(record: Any) -> bool:
    rec_type = _type_name(record)
    if rec_type is not None and rec_type != "RIGHTSIZING_MEMORY":
        return False
    return _raises(
        getattr(record, "recommended_memory_request_bytes", None),
        getattr(record, "current_memory_request_bytes", None),
    )


def _memory_limit_change(member: Any) -> tuple[Optional[int], Optional[int]]:
    """``(current, proposed)`` memory limit of a change member; ``proposed`` is None without a set limit edit."""
    evidence = getattr(member, "evidence", None)
    current = getattr(getattr(evidence, "current", None), "memory_limit_bytes", None)
    current = current if isinstance(current, int) and not isinstance(current, bool) and current > 0 else None
    proposed: Optional[int] = None
    for change in getattr(evidence, "limit_changes", None) or []:
        if getattr(change, "resource", None) != "memory" or getattr(change, "action", None) != "set":
            continue
        value = getattr(change, "proposed", None)
        if isinstance(value, int) and not isinstance(value, bool):
            proposed = value
    return current, proposed


def _limit_cannot_cure_oom(member: Any) -> bool:
    """A memory increase that left a known memory limit in place (or lowered it): it cannot stop OOM kills.

    The kernel OOM-kills at the memory **limit**: only a raised limit can lower the
    kill rate. Such a change keeps the strict "any kill fails" gate instead of the
    rate comparison, which assumes the change could cure the kills.
    """
    if not raises_memory_request(member):
        return False
    current, proposed = _memory_limit_change(member)
    if current is None:
        return False
    return proposed is None or proposed <= current


#: Baseline exposure of the OOM rate: from the workload's first observation in the evidence window.
EXPOSURE_FIRST_OBSERVED = "workload_first_observed"
#: Fallback when the first observation is unknown: the whole evidence window (the cluster's data span).
EXPOSURE_EVIDENCE_WINDOW = "evidence_window"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _baseline_exposure(evidence: Any) -> tuple[Optional[float], str]:
    """Seconds the target was observed before the change, and where they come from.

    The evidence window starts at the cluster's data start, not at the workload's:
    a workload created 40 minutes before the scan was never exposed for the hours
    before it. Its exposure runs from its first observation (bounded by the window)
    to the window end. Without a usable first observation, the whole window.
    """
    window = _number(getattr(evidence, "observation_window_seconds", None))
    window = window if window is not None and window > 0 else None
    first = getattr(evidence, "first_observed_at", None)
    end = getattr(evidence, "observation_window_end", None)
    start = getattr(evidence, "observation_window_start", None)
    if isinstance(first, datetime):
        if not isinstance(end, datetime) and isinstance(start, datetime) and window is not None:
            end = ensure_utc(start) + timedelta(seconds=window)
        if isinstance(end, datetime):
            exposure = (ensure_utc(end) - ensure_utc(first)).total_seconds()
            if window is not None:
                exposure = min(exposure, window)
            if exposure > 0:
                return exposure, EXPOSURE_FIRST_OBSERVED
    return window, EXPOSURE_EVIDENCE_WINDOW


def _oom_baseline(group: Sequence[Any]) -> tuple[Optional[int], Optional[float], Optional[str]]:
    """OOM kills observed before the change by the memory increases of one container (and their exposure).

    Returns the kills, the seconds the container was observed before the change
    (the longest of its members, see :func:`_baseline_exposure`) and the source of
    that exposure (``workload_first_observed`` or the ``evidence_window`` fallback).
    """
    events: Optional[int] = None
    seconds: Optional[float] = None
    source: Optional[str] = None
    for member in group:
        if not raises_memory_request(member):
            continue
        evidence = getattr(member, "evidence", None)
        count = getattr(evidence, "oom_events", None)
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        events = max(count, events or 0)
        exposure, exposure_source = _baseline_exposure(evidence)
        if exposure is not None and exposure > (seconds or 0.0):
            seconds, source = exposure, exposure_source
    return events, seconds, source


def _judge_container(
    group: Sequence[Any],
    gates: Sequence[HealthGateResult],
    post_start: datetime,
    post_end: datetime,
    oom_restarts: Optional[int],
) -> tuple[List[HealthGateResult], Optional[ReliabilityOutcome], bool]:
    """Replace the ``oom_kills`` gate of one container by the rate comparison when allowed.

    Returns the gates, the outcome and whether the gate was replaced (the
    container's post-window OOM restarts are then judged by the rate, not by the
    "any kill" or restart gates).
    """
    if not any(raises_memory_request(member) for member in group):
        return list(gates), None, False
    baseline_events, baseline_seconds, exposure_source = _oom_baseline(group)
    post_events = oom_restarts if isinstance(oom_restarts, int) and not isinstance(oom_restarts, bool) else None
    outcome = oom_rate_outcome(
        baseline_events,
        baseline_seconds,
        post_events,
        (ensure_utc(post_end) - ensure_utc(post_start)).total_seconds(),
        baseline_exposure_source=exposure_source,
    )
    if any(_lowers_memory_request(member) or _limit_cannot_cure_oom(member) for member in group):
        return list(gates), outcome, False
    if not outcome.baseline_events or post_events is None:
        return list(gates), outcome, False
    if not any(gate.gate == OOM_GATE and gate.value is not None for gate in gates):
        # The health gate could not read OOM kills: keep it not_available, never a pass.
        return list(gates), outcome, False
    failed = outcome.outcome is ChangeOutcome.NOT_REALIZED and post_events > 0
    rate_gate = HealthGateResult(
        gate=OOM_GATE,
        status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
        value=post_events,
        threshold=outcome.baseline_events,
        detail=f"reliability increase: {outcome.reason}",
    )
    return [*(gate for gate in gates if gate.gate != OOM_GATE), rate_gate], outcome, True


def _excuse_oom_restarts(gates: Sequence[HealthGateResult], excused: int) -> List[HealthGateResult]:
    """Re-judge the ``restarts`` gate without ``excused`` OOM restarts already judged by a rate gate.

    Every OOM kill also restarts its container: once the kill rate of an
    OOM-driven increase is judged, the same kills must not fail the restart gate.
    Any other restart still counts against ``VERIFICATION_MAX_RESTART_DELTA``.
    """
    if excused <= 0:
        return list(gates)
    judged: List[HealthGateResult] = []
    for gate in gates:
        if gate.gate != RESTART_GATE or gate.value is None or gate.threshold is None:
            judged.append(gate)
            continue
        total = int(gate.value)
        other = max(total - excused, 0)
        judged.append(
            HealthGateResult(
                gate=RESTART_GATE,
                status=HealthGateStatus.FAILED if other > gate.threshold else HealthGateStatus.PASSED,
                value=other,
                threshold=gate.threshold,
                detail=(
                    f"reliability increase: {other} restart(s) besides {min(excused, total)} OOM kill(s) "
                    f"judged by the OOM-kill rate (restart delta {total}, threshold {gate.threshold:g})"
                ),
            )
        )
    return judged


def container_reliability(
    group: Sequence[Any],
    gates: Sequence[HealthGateResult],
    post_start: datetime,
    post_end: datetime,
    oom_restarts: Optional[int] = None,
) -> tuple[List[HealthGateResult], Optional[ReliabilityOutcome]]:
    """Health gates and reliability outcome of **one container** of a change (H1).

    ``group`` holds the change members of that container, ``gates`` the health
    gates read for that container only and ``oom_restarts`` the container's OOM
    restarts after the change, counted like ``evidence.oom_events`` (restarts whose
    termination was ``OOMKilled``, summed over pods). For a memory-request increase
    made because of OOM kills, a kill after the rollout does not mean the change
    hurt: the question is whether the kill **rate** dropped. That container's
    ``oom_kills`` gate is then replaced by one rate comparison, which fails only
    when the rate did not drop (a person reviews it), and its ``restarts`` gate no
    longer counts those OOM restarts. Everything else keeps the strict gates:
    containers without a memory increase (CPU-only increases cannot cure OOM kills,
    and decreases must never hide one), containers with a memory decrease, memory
    increases that kept or lowered a known memory limit (the kernel kills at the
    limit, so the request alone cannot cure the kills), memory increases without
    OOM history, and an unknown OOM restart count (fail closed).
    """
    judged, outcome, relaxed = _judge_container(group, gates, post_start, post_end, oom_restarts)
    if relaxed and outcome is not None and outcome.post_events:
        judged = _excuse_oom_restarts(judged, outcome.post_events)
    return judged, outcome


def increase_reliability(
    members: Sequence[Any],
    gates_by_container: Mapping[Optional[str], Sequence[HealthGateResult]],
    post_start: datetime,
    post_end: datetime,
    oom_restarts_by_container: Optional[Mapping[Optional[str], Optional[int]]] = None,
) -> tuple[Dict[Optional[str], List[HealthGateResult]], Optional[ReliabilityOutcome]]:
    """Apply :func:`container_reliability` to every container of a change.

    ``gates_by_container`` maps each container to the gates read for it and
    ``oom_restarts_by_container`` to its OOM restarts after the change. Returns the
    gates per container (OOM gates replaced only where :func:`container_reliability`
    allows it) and the change-level reliability outcome: the OOM-kill rate summed
    over the containers whose memory request was raised (``None`` without any).
    Restart counters are read per pod, so the OOM restarts judged by a rate gate are
    left out of the ``restarts`` gate of every container of the change.
    """
    judged, outcome, _ = increase_reliability_detail(
        members, gates_by_container, post_start, post_end, oom_restarts_by_container
    )
    return judged, outcome


def increase_reliability_detail(
    members: Sequence[Any],
    gates_by_container: Mapping[Optional[str], Sequence[HealthGateResult]],
    post_start: datetime,
    post_end: datetime,
    oom_restarts_by_container: Optional[Mapping[Optional[str], Optional[int]]] = None,
) -> tuple[Dict[Optional[str], List[HealthGateResult]], Optional[ReliabilityOutcome], frozenset]:
    """:func:`increase_reliability` plus the containers whose OOM restarts left the restart gates.

    The third value lets the baseline comparison leave the same containers'
    baseline OOM restarts out of the baseline restart count (MEA-07 review).
    """
    oom_restarts = dict(oom_restarts_by_container or {})
    by_container: Dict[Optional[str], List[Any]] = {}
    for member in members:
        by_container.setdefault(getattr(member, "container_name", None), []).append(member)
    result: Dict[Optional[str], List[HealthGateResult]] = {}
    outcomes: Dict[Optional[str], ReliabilityOutcome] = {}
    excused = 0
    relaxed_containers: set = set()
    for container, gates in gates_by_container.items():
        result[container], outcome, relaxed = _judge_container(
            by_container.get(container, []), gates, post_start, post_end, oom_restarts.get(container)
        )
        if outcome is not None:
            outcomes[container] = outcome
            if relaxed:
                relaxed_containers.add(container)
                excused += outcome.post_events or 0
    for container, group in by_container.items():
        if container not in gates_by_container:
            _, outcome, _ = _judge_container(group, [], post_start, post_end, oom_restarts.get(container))
            if outcome is not None:
                outcomes[container] = outcome
    if excused:
        result = {container: _excuse_oom_restarts(gates, excused) for container, gates in result.items()}
    relaxed_set = frozenset(relaxed_containers)
    if not outcomes:
        return result, None, relaxed_set
    if len(outcomes) == 1:
        return result, next(iter(outcomes.values())), relaxed_set
    baselines = [outcome.baseline_events for outcome in outcomes.values()]
    posts = [outcome.post_events for outcome in outcomes.values()]
    longest = max(
        (outcome for outcome in outcomes.values() if outcome.baseline_hours),
        key=lambda outcome: outcome.baseline_hours or 0.0,
        default=None,
    )
    combined = oom_rate_outcome(
        sum(value for value in baselines if value is not None) if any(v is not None for v in baselines) else None,
        longest.baseline_hours * 3600.0 if longest is not None and longest.baseline_hours else None,
        None if any(value is None for value in posts) else sum(value for value in posts if value is not None),
        (ensure_utc(post_end) - ensure_utc(post_start)).total_seconds(),
        baseline_exposure_source=longest.baseline_exposure_source if longest is not None else None,
    )
    return result, combined, relaxed_set
