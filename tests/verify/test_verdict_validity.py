"""Validity of the change verdict statistics (MEA-01, MEA-09).

The Monte-Carlo tests run the real per-quantity fit of
:class:`RequestRightsizingStrategy` (hour-of-week counterfactual, circular
block bootstrap, between-week term, outcome rule) on seeded synthetic series.
With a true saving of zero the share of ``realized`` verdicts must stay near
the nominal one-sided rate of the 90% interval (5%).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List

import pytest

from greenkube.verify.model import ChangeOutcome
from greenkube.verify.statistics import (
    circular_block_bootstrap,
    outcome_rule,
    student_t_quantile,
    week_level_deviations,
)
from greenkube.verify.strategy import MeasurementSettings, QuantitySpec, RequestRightsizingStrategy

SPEC = QuantitySpec("allocated_cost", "currency", True, "allocated cost")
#: A Monday, so baseline and post-window weeks start on the same hour of the week.
DECISION = datetime(2026, 6, 1, tzinfo=timezone.utc)
TRIALS = 400
WEEK = 168


# --------------------------------------------------------------------------- #
# MEA-09: the noise floor applies to "realized" too                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("low", "high", "epsilon", "expected"),
    [
        (0.0001, 0.0001, 0.0008, ChangeOutcome.NOT_REALIZED),
        (0.0005, 0.002, 0.0008, ChangeOutcome.INCONCLUSIVE),
        (0.02, 0.02, 0.0008, ChangeOutcome.REALIZED),
    ],
)
def test_outcome_rule_needs_the_lower_bound_above_the_noise_floor(low, high, epsilon, expected):
    assert outcome_rule(low, high, epsilon) is expected


def _fit(pre: Dict[datetime, float], post: Dict[datetime, float], settings: MeasurementSettings, seed: object):
    return RequestRightsizingStrategy._fit_basis(
        SPEC,
        pre,
        post,
        None,
        base={"quantity": SPEC.quantity, "unit": SPEC.unit, "claimed": True, "projected_annual": None},
        load=None,
        expected_hours=len(post),
        projected=None,
        settings=settings,
        seed_parts=("sim", seed),
    )


def _hours(start: datetime, count: int) -> List[datetime]:
    return [start + timedelta(hours=index) for index in range(count)]


def _windows(pre_days: int, values: List[float]) -> tuple[Dict[datetime, float], Dict[datetime, float]]:
    pre_hours = _hours(DECISION - timedelta(days=pre_days), pre_days * 24)
    post_hours = _hours(DECISION, WEEK)
    assert len(values) == len(pre_hours) + len(post_hours)
    pre = dict(zip(pre_hours, values[: len(pre_hours)]))
    post = dict(zip(post_hours, values[len(pre_hours) :]))
    return pre, post


def test_a_tiny_deterministic_saving_below_the_floor_is_not_realized():
    # Sim D1: requests -0.3% on a fixed-price workload; the noise floor is 2%.
    pre, post = _windows(7, [1.0] * WEEK + [0.997] * WEEK)
    fitted = _fit(pre, post, MeasurementSettings(), "d1")
    assert fitted.estimate.outcome is ChangeOutcome.NOT_REALIZED
    assert "negligible" in (fitted.estimate.reason or "")


@pytest.mark.parametrize("jitter", [1e-7, 1e-6, 1e-5])
def test_float_jitter_on_a_fixed_price_baseline_is_still_deterministic(jitter):
    # Review: an exactly flat baseline was realized with a zero-width interval while a
    # relative jitter of 1e-6 (float noise of a price refresh) made the same change inconclusive.
    rng = random.Random(f"jitter-{jitter}")
    values = [1.0 + rng.gauss(0, jitter) for _ in range(WEEK)] + [0.9 + rng.gauss(0, jitter) for _ in range(WEEK)]
    pre, post = _windows(7, values)
    fitted = _fit(pre, post, MeasurementSettings(), ("jitter", jitter))
    assert fitted.interval_params is not None
    assert fitted.interval_params["between_week_basis"] == "deterministic_baseline"
    assert fitted.estimate.outcome is ChangeOutcome.REALIZED


@pytest.mark.parametrize("noise", [1e-3, 1e-2])
def test_a_baseline_with_real_relative_variation_uses_the_prior(noise):
    rng = random.Random(f"noise-{noise}")
    values = [1.0 + rng.gauss(0, noise) for _ in range(WEEK)] + [0.9 + rng.gauss(0, noise) for _ in range(WEEK)]
    pre, post = _windows(7, values)
    fitted = _fit(pre, post, MeasurementSettings(), ("noise", noise))
    assert fitted.interval_params is not None
    assert fitted.interval_params["between_week_basis"] == "prior"
    assert fitted.estimate.outcome is ChangeOutcome.INCONCLUSIVE


def test_a_deterministic_saving_is_realized_with_a_zero_width_interval():
    pre, post = _windows(7, [1.0] * WEEK + [0.7] * WEEK)
    estimate = _fit(pre, post, MeasurementSettings(), "det").estimate
    assert estimate.outcome is ChangeOutcome.REALIZED
    assert estimate.ci_low is not None and estimate.ci_high is not None
    assert estimate.ci_high - estimate.ci_low == pytest.approx(0.0, abs=1e-9)
    assert estimate.rate_per_hour == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# MEA-01: seeded Monte-Carlo false-positive and power tests                    #
# --------------------------------------------------------------------------- #


def _iid(rng: random.Random, weeks: int) -> List[float]:
    return [1.0 + rng.gauss(0, 0.1) for _ in range(weeks * WEEK)]


def _ar1(rng: random.Random, weeks: int) -> List[float]:
    phi, sd = 0.9, 0.1
    x = rng.gauss(0, sd)
    out = []
    for _ in range(weeks * WEEK):
        x = phi * x + rng.gauss(0, sd * math.sqrt(1 - phi * phi))
        out.append(1.0 + x)
    return out


def _week_level_cv(cv: float) -> Callable[[random.Random, int], List[float]]:
    def generate(rng: random.Random, weeks: int) -> List[float]:
        out: List[float] = []
        for _ in range(weeks):
            level = rng.gauss(0, cv)
            out.extend(1.0 + level + rng.gauss(0, 0.02) for _ in range(WEEK))
        return out

    return generate


_week_level = _week_level_cv(0.05)


SCENARIOS: Dict[str, Callable[[random.Random, int], List[float]]] = {
    "iid": _iid,
    "ar1_0.9": _ar1,
    "week_level_5pct": _week_level,
}
#: Week-to-week variation larger than the 5% prior: only an estimated term (K >= 2) covers it.
WIDE_SCENARIOS: Dict[str, Callable[[random.Random, int], List[float]]] = {
    "week_level_10pct": _week_level_cv(0.10),
    "week_level_20pct": _week_level_cv(0.20),
}
SCENARIOS.update(WIDE_SCENARIOS)
NARROW_SCENARIOS = ["iid", "ar1_0.9", "week_level_5pct"]


def _rates(scenario: str, pre_days: int, settings: MeasurementSettings, saving: float = 0.0) -> Dict[str, float]:
    rng = random.Random(f"{scenario}-{pre_days}-{saving}")
    weeks = pre_days // 7 + 1
    counts = {outcome: 0 for outcome in ChangeOutcome}
    for trial in range(TRIALS):
        values = SCENARIOS[scenario](rng, weeks)
        cut = len(values) - WEEK
        values = values[:cut] + [value * (1.0 - saving) for value in values[cut:]]
        pre, post = _windows(pre_days, values)
        counts[_fit(pre, post, settings, (scenario, trial)).estimate.outcome] += 1
    return {outcome.value: count / TRIALS for outcome, count in counts.items()}


FAST = MeasurementSettings(bootstrap_samples=200)
NO_FLOOR = MeasurementSettings(bootstrap_samples=200, epsilon_ratio=0.0)


@pytest.mark.parametrize("pre_days", [7, 28])
@pytest.mark.parametrize("scenario", NARROW_SCENARIOS)
def test_a_zero_saving_is_rarely_declared_realized(scenario, pre_days):
    assert _rates(scenario, pre_days, FAST)["realized"] <= 0.07


@pytest.mark.parametrize("scenario", sorted(WIDE_SCENARIOS))
def test_a_28_day_baseline_keeps_the_false_positive_rate_with_large_week_to_week_variation(scenario):
    # The prior (5%) is not used with K = 4 baseline weeks: the estimated term covers 10% and 20%.
    assert _rates(scenario, 28, FAST)["realized"] <= 0.055


def test_known_limitation_one_baseline_week_understates_week_to_week_variation_above_the_prior():
    """KNOWN LIMITATION (documented in docs/methodology/savings-measurement.md, section 6).

    With the default 7-day baseline the between-week term is the 5% prior. A
    quantity whose weekly level varies by 10% is declared ``realized`` with a
    true saving of zero far more often than the nominal 5% (review simulation:
    16%; 33.5% at 20%). This test pins the limitation so that a change of the
    default baseline length, or of the prior, is a visible decision.
    """
    rate = _rates("week_level_10pct", 7, FAST)["realized"]
    assert rate > 0.07


@pytest.mark.parametrize("scenario", NARROW_SCENARIOS)
def test_the_interval_alone_keeps_the_nominal_false_positive_rate(scenario):
    # Without the noise floor the one-sided false-realized rate of a 90% interval is 5%.
    rate = _rates(scenario, 28, NO_FLOOR)["realized"]
    assert rate <= 0.07


def test_the_prior_week_to_week_variation_is_calibrated_for_a_single_baseline_week():
    # A 5% week-level shift with one baseline week: the prior (5%) carries the whole between-week term.
    rate = _rates("week_level_5pct", 7, NO_FLOOR)["realized"]
    assert 0.02 <= rate <= 0.08


@pytest.mark.parametrize("pre_days", [7, 28])
def test_a_true_thirty_percent_saving_with_week_level_noise_is_realized(pre_days):
    assert _rates("week_level_5pct", pre_days, FAST, saving=0.30)["realized"] >= 0.90


# --------------------------------------------------------------------------- #
# Building blocks                                                              #
# --------------------------------------------------------------------------- #


def test_student_t_quantile_matches_tables():
    assert student_t_quantile(0.95, 1) == pytest.approx(6.3138, abs=1e-3)
    assert student_t_quantile(0.95, 3) == pytest.approx(2.3534, abs=1e-3)
    assert student_t_quantile(0.95, 6) == pytest.approx(1.9432, abs=1e-3)
    assert student_t_quantile(0.95, 30) == pytest.approx(1.6973, abs=1e-3)
    assert student_t_quantile(0.95, math.inf) == pytest.approx(1.6449, abs=1e-3)


def test_circular_block_bootstrap_resamples_every_hour_equally_and_reports_its_standard_error():
    rng = random.Random(5)
    values = [rng.gauss(0, 1) for _ in range(WEEK)]
    interval = circular_block_bootstrap(values, seed=1, samples=400)
    assert interval.se is not None and interval.se > 0
    assert interval.dof == 6
    assert interval.low is not None and interval.high is not None
    assert interval.low < interval.mean < interval.high


def test_week_level_deviations_need_two_complete_weeks():
    end = DECISION
    hours = _hours(end - timedelta(days=14), 2 * WEEK)
    residuals = {hour: (0.1 if index < WEEK else -0.1) for index, hour in enumerate(hours)}
    assert week_level_deviations(residuals, end) == pytest.approx([0.1, -0.1])
    assert week_level_deviations(dict(list(residuals.items())[:WEEK]), end) == pytest.approx([0.1])


def test_counterfactual_params_record_the_interval_method():
    pre, post = _windows(28, _week_level(random.Random(3), 5))
    params = _fit(pre, post, FAST, "params").interval_params
    assert params is not None
    assert params["interval_method"] == "circular_block_bootstrap_t_with_between_week"
    assert params["baseline_weeks"] == 4
    assert params["between_week_basis"] == "estimated"
    assert RequestRightsizingStrategy.version == "2.3"
