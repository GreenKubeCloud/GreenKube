"""OLS load-regression counterfactual (C3 v2) on synthetic series with known effects.

Every scenario builds a workload whose hourly cost follows a seasonal profile
plus a load term and noise, applies a known saving after the change, and
checks that the estimator recovers it inside its 90% interval (or falls back
to the hour-of-week MVP, with the reason recorded, when its diagnostics fail).
"""

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict

import pytest

from greenkube.verify.load_driver import ResolvedLoad
from greenkube.verify.model import ChangeOutcome, LoadDriverInfo, TimeWindow
from greenkube.verify.regression import (
    METHOD_OLS,
    SEASONALITY_DAY_TYPE,
    SEASONALITY_HOUR_OF_WEEK,
    OlsCounterfactual,
    residual_bootstrap,
)
from greenkube.verify.series import floor_hour, hourly_buckets, window_hours
from greenkube.verify.strategy import MVP_METHOD, MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, workload_rows  # pyrefly: ignore[missing-import]

STEP = 300
SETTINGS = MeasurementSettings(bootstrap_samples=400)
STRATEGY = RequestRightsizingStrategy()
INFO = LoadDriverInfo(key="istio_rps", label="Istio requests/s", coverage_pre=1.0, coverage_post=1.0)
#: Fixed rollout time (a Monday noon): the seasonal alignment of every scenario, and with it the
#: stochastic outcome of each seed, must not depend on when the suite runs. The estimator reads no clock.
COMPLETED = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def _seasonal(moment: datetime) -> float:
    """Daily traffic shape: busy afternoons, quiet nights, lighter weekends."""
    base = 100.0 + 60.0 * math.sin((moment.hour - 6) / 24 * 2 * math.pi)
    return base * (0.7 if moment.weekday() >= 5 else 1.0)


def _load_series(window: TimeWindow, rng: random.Random, scale: float = 1.0) -> Dict[datetime, float]:
    """Seasonal load with day-to-day variation (so it varies inside every seasonal slot)."""
    values: Dict[datetime, float] = {}
    day_factor = 1.0
    for hour in window_hours(window.model_copy(update={"start": floor_hour(window.start)})):
        if hour.hour == 0 or not values:
            day_factor = rng.uniform(0.75, 1.25)
        values[hour] = scale * _seasonal(hour) * day_factor * rng.uniform(0.95, 1.05)
    return values


def _scenario(
    *,
    effect: float,
    seed: int,
    noise: float = 0.002,
    slope: float = 0.0002,
    post_scale: float = 1.1,
    ar: float = 0.0,
    pre_days: float = 7.0,
    prior_week_cv: float = 0.05,
):
    """Per-pod hourly cost = 0.02 + slope x load + AR(1) noise, minus ``effect`` per pod after the change."""
    rng = random.Random(seed)
    settings = MeasurementSettings(bootstrap_samples=400, pre_window_days=pre_days, prior_week_cv=prior_week_cv)
    change = change_event(COMPLETED)
    plan = STRATEGY.required_windows(change, settings)
    pre_load = _load_series(plan.pre, rng)
    post_load = _load_series(plan.post, rng, scale=post_scale)
    state = {"previous": 0.0}

    def cost_for(load: Dict[datetime, float], cut: float) -> Callable[[datetime], float]:
        cache: Dict[datetime, float] = {}

        def cost(moment: datetime) -> float:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            if hour not in cache:
                state["previous"] = ar * state["previous"] + rng.gauss(0.0, noise)
                cache[hour] = 0.02 + slope * load[hour] + state["previous"] - cut
            return cache[hour]

        return cost

    pre = hourly_buckets(workload_rows(plan.pre, pods=("api-a",), cost=cost_for(pre_load, 0.0)), plan.pre, STEP)
    post = hourly_buckets(workload_rows(plan.post, pods=("api-a",), cost=cost_for(post_load, effect)), plan.post, STEP)
    load = ResolvedLoad(info=INFO, pre=pre_load, post=post_load)
    result = STRATEGY.estimate(
        change,
        plan,
        pre,
        post,
        load=load,
        confounders=[],
        projections={},
        settings=settings,
        seed_parts=(change.change_id, seed),
    )
    return result, {item.quantity: item for item in result.estimates}


def test_load_regression_recovers_a_known_saving_within_its_interval():
    # The synthetic weeks share one level: no prior week-to-week variation (MEA-01) is assumed,
    # so the interval is the regression's own.
    result, estimates = _scenario(effect=0.004, seed=3, prior_week_cv=0.0)
    cost = estimates["allocated_cost"]
    assert cost.method == METHOD_OLS
    assert result.counterfactual_params["model"] == METHOD_OLS
    diagnostics = result.counterfactual_params["diagnostics"]["allocated_cost"]
    assert diagnostics["passed"] is True
    assert diagnostics["seasonality"] == SEASONALITY_DAY_TYPE
    assert diagnostics["load_coefficient"] == pytest.approx(0.0002, rel=0.15)
    assert cost.ci_low is not None and cost.ci_high is not None
    assert cost.ci_low <= 0.004 <= cost.ci_high
    assert cost.ci_low > 0
    assert result.outcome is ChangeOutcome.REALIZED


def test_load_growth_is_not_credited_as_a_saving():
    """A busier post-window raises the counterfactual instead of hiding the saving or inventing one."""
    _, estimates = _scenario(effect=0.0, seed=5, post_scale=1.2)
    cost = estimates["allocated_cost"]
    assert cost.method == METHOD_OLS
    assert cost.ci_low is not None and cost.ci_high is not None
    assert cost.ci_low <= 0.0 <= cost.ci_high or abs(cost.rate_per_hour or 0.0) < 0.001
    assert cost.outcome is not ChangeOutcome.REALIZED


def test_interval_covers_the_true_effect_at_about_its_nominal_rate():
    """Over 20 synthetic workloads with autocorrelated noise the 90% interval holds the true saving."""
    covered = 0
    for seed in range(20):
        _, estimates = _scenario(effect=0.003, seed=100 + seed, noise=0.003, ar=0.5)
        cost = estimates["allocated_cost"]
        assert cost.method == METHOD_OLS
        assert cost.ci_low is not None and cost.ci_high is not None
        covered += cost.ci_low <= 0.003 <= cost.ci_high
    assert covered >= 15


def test_two_week_baseline_uses_hour_of_week_seasonality():
    result, estimates = _scenario(effect=0.004, seed=8, pre_days=14)
    assert estimates["allocated_cost"].method == METHOD_OLS
    assert result.counterfactual_params["diagnostics"]["allocated_cost"]["seasonality"] == SEASONALITY_HOUR_OF_WEEK


def test_extrapolated_load_falls_back_to_the_hour_of_week_mean():
    result, estimates = _scenario(effect=0.004, seed=9, post_scale=3.0)
    cost = estimates["allocated_cost"]
    assert cost.method == MVP_METHOD
    diagnostics = result.counterfactual_params["diagnostics"]["allocated_cost"]
    assert diagnostics["passed"] is False and diagnostics["fallback"] == MVP_METHOD
    assert any("outside the baseline range" in failure for failure in diagnostics["failures"])


def test_negative_load_slope_falls_back():
    result, estimates = _scenario(effect=0.004, seed=10, slope=-0.0002)
    assert estimates["allocated_cost"].method == MVP_METHOD
    failures = result.counterfactual_params["diagnostics"]["allocated_cost"]["failures"]
    assert any("falls as the load grows" in failure for failure in failures)


def test_without_a_load_driver_the_mvp_is_used_without_diagnostics():
    change = change_event(COMPLETED)
    plan = STRATEGY.required_windows(change, SETTINGS)
    pre = hourly_buckets(workload_rows(plan.pre), plan.pre, 300)
    post = hourly_buckets(workload_rows(plan.post, cpu_request=300), plan.post, 300)
    result = STRATEGY.estimate(
        change,
        plan,
        pre,
        post,
        load=None,
        confounders=[],
        projections={},
        settings=SETTINGS,
        seed_parts=("c", 1),
    )
    cost = next(item for item in result.estimates if item.quantity == "allocated_cost")
    assert cost.method == MVP_METHOD
    assert result.counterfactual_params["model"] == MVP_METHOD
    assert result.counterfactual_params["diagnostics"] == {}


def test_load_without_within_slot_variation_is_not_identifiable():
    start = COMPLETED - timedelta(days=20)
    hours = [start + timedelta(hours=index) for index in range(168)]
    load = {hour: _seasonal(hour) for hour in hours}
    values = {hour: 1.0 + 0.01 * load[hour] for hour in hours}
    model = OlsCounterfactual.fit(values, load)
    assert not model.diagnostics.passed
    assert any("not identifiable" in failure for failure in model.diagnostics.failures)


def test_residual_bootstrap_widens_the_interval_with_baseline_uncertainty():
    rng = random.Random(4)
    savings = [0.5 + rng.gauss(0, 0.05) for _ in range(168)]
    residuals = [rng.gauss(0, 0.5) for _ in range(168)]
    narrow = residual_bootstrap(savings, residuals, [0.0] * 168, dof=100, seed=1, samples=400)
    wide = residual_bootstrap(savings, residuals, [1.0 / 168] * 168, dof=100, seed=1, samples=400)
    assert narrow.mean == wide.mean
    assert narrow.low is not None and narrow.high is not None and wide.low is not None and wide.high is not None
    assert (wide.high - wide.low) > (narrow.high - narrow.low)
    again = residual_bootstrap(savings, residuals, [1.0 / 168] * 168, dof=100, seed=1, samples=400)
    assert again == wide
