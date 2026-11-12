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
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from greenkube.impact.types import CO2E_COMPONENTS, MEASURED_CO2E_COMPONENTS
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


# --------------------------------------------------------------------------- #
# Reliability outcome of request increases (H1)                                #
# --------------------------------------------------------------------------- #

#: One-sided significance of an OOM-rate drop (matches the 90% intervals of the savings estimates).
RELIABILITY_ALPHA = 0.10
#: Health gate replaced by the rate comparison for increases.
OOM_GATE = "oom_kills"


@dataclass(frozen=True)
class ReliabilityOutcome:
    """OOM-kill rate before and after a request increase (H1): the outcome of a reliability fix.

    ``baseline_events`` are the OOM kills the recommendation observed over its
    evidence window (``evidence.oom_events``); ``post_events`` the kills the health
    gates read over the post-change window. If the rate had not changed, the
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


def _oom_baseline(group: Sequence[Any]) -> tuple[Optional[int], Optional[float]]:
    """OOM kills observed before the change by the memory increases of one container (and their window)."""
    events: Optional[int] = None
    seconds: Optional[float] = None
    for member in group:
        if not raises_memory_request(member):
            continue
        evidence = getattr(member, "evidence", None)
        count = getattr(evidence, "oom_events", None)
        window = getattr(evidence, "observation_window_seconds", None)
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        events = max(count, events or 0)
        if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
            seconds = max(float(window), seconds or 0.0)
    return events, seconds


def _post_oom_events(gates: Sequence[HealthGateResult]) -> Optional[int]:
    values = [gate.value for gate in gates if gate.gate == OOM_GATE]
    if not values or any(value is None for value in values):
        return None
    return int(math.fsum(value for value in values if value is not None))


def container_reliability(
    group: Sequence[Any], gates: Sequence[HealthGateResult], post_start: datetime, post_end: datetime
) -> tuple[List[HealthGateResult], Optional[ReliabilityOutcome]]:
    """Health gates and reliability outcome of **one container** of a change (H1).

    ``group`` holds the change members of that container and ``gates`` the health
    gates read for that container only. For a memory-request increase made because
    of OOM kills, a kill after the rollout does not mean the change hurt: the
    question is whether the kill **rate** dropped. That container's ``oom_kills``
    gate is then replaced by one rate comparison, which fails only when the rate did
    not drop (a person reviews it). Everything else keeps the strict "any kill
    fails" gate: containers without a memory increase (CPU-only increases cannot
    cure OOM kills, and decreases must never hide one), containers with a memory
    decrease, and memory increases without OOM history.
    """
    if not any(raises_memory_request(member) for member in group):
        return list(gates), None
    baseline_events, baseline_seconds = _oom_baseline(group)
    post_events = _post_oom_events(gates)
    outcome = oom_rate_outcome(
        baseline_events,
        baseline_seconds,
        post_events,
        (ensure_utc(post_end) - ensure_utc(post_start)).total_seconds(),
    )
    if any(_lowers_memory_request(member) for member in group):
        return list(gates), outcome
    if not outcome.baseline_events or post_events is None:
        return list(gates), outcome
    failed = outcome.outcome is ChangeOutcome.NOT_REALIZED and post_events > 0
    rate_gate = HealthGateResult(
        gate=OOM_GATE,
        status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
        value=post_events,
        threshold=outcome.baseline_events,
        detail=f"reliability increase: {outcome.reason}",
    )
    return [*(gate for gate in gates if gate.gate != OOM_GATE), rate_gate], outcome


def increase_reliability(
    members: Sequence[Any],
    gates_by_container: Mapping[Optional[str], Sequence[HealthGateResult]],
    post_start: datetime,
    post_end: datetime,
) -> tuple[Dict[Optional[str], List[HealthGateResult]], Optional[ReliabilityOutcome]]:
    """Apply :func:`container_reliability` to every container of a change.

    ``gates_by_container`` maps each container to the gates read for it. Returns the
    gates per container (OOM gates replaced only where :func:`container_reliability`
    allows it) and the change-level reliability outcome: the OOM-kill rate summed
    over the containers whose memory request was raised (``None`` without any).
    """
    by_container: Dict[Optional[str], List[Any]] = {}
    for member in members:
        by_container.setdefault(getattr(member, "container_name", None), []).append(member)
    result: Dict[Optional[str], List[HealthGateResult]] = {}
    outcomes: Dict[Optional[str], ReliabilityOutcome] = {}
    for container, gates in gates_by_container.items():
        result[container], outcome = container_reliability(by_container.get(container, []), gates, post_start, post_end)
        if outcome is not None:
            outcomes[container] = outcome
    for container, group in by_container.items():
        if container not in gates_by_container:
            _, outcome = container_reliability(group, [], post_start, post_end)
            if outcome is not None:
                outcomes[container] = outcome
    if not outcomes:
        return result, None
    if len(outcomes) == 1:
        return result, next(iter(outcomes.values()))
    baselines = [outcome.baseline_events for outcome in outcomes.values()]
    posts = [outcome.post_events for outcome in outcomes.values()]
    hours = [outcome.baseline_hours for outcome in outcomes.values() if outcome.baseline_hours]
    return result, oom_rate_outcome(
        sum(value for value in baselines if value is not None) if any(v is not None for v in baselines) else None,
        max(hours) * 3600.0 if hours else None,
        None if any(value is None for value in posts) else sum(value for value in posts if value is not None),
        (ensure_utc(post_end) - ensure_utc(post_start)).total_seconds(),
    )
