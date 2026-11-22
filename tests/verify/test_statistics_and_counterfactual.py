"""Moving-block bootstrap, outcome rule and hour-of-week counterfactual (C3, C4)."""

import random
from datetime import timedelta

import pytest

from greenkube.verify.counterfactual import HourOfWeekCounterfactual, align, load_elasticity
from greenkube.verify.model import ChangeOutcome
from greenkube.verify.statistics import moving_block_bootstrap, outcome_rule, seed_for
from tests.verify.fixtures import hour_now  # pyrefly: ignore[missing-import]


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    rng = random.Random(1)
    values = [1.0 + rng.gauss(0, 0.5) for _ in range(168)]
    first = moving_block_bootstrap(values, seed=seed_for("change", 1), samples=400)
    second = moving_block_bootstrap(values, seed=seed_for("change", 1), samples=400)
    assert first == second
    assert first.low is not None and first.high is not None
    assert first.low < first.mean < first.high
    assert first.block == 24


def test_bootstrap_of_a_constant_series_is_degenerate():
    interval = moving_block_bootstrap([0.5] * 48, seed=3, samples=200)
    assert interval.mean == pytest.approx(0.5)
    assert interval.low == pytest.approx(0.5) and interval.high == pytest.approx(0.5)


def test_bootstrap_without_enough_values_has_no_interval():
    assert moving_block_bootstrap([], seed=1).low is None
    single = moving_block_bootstrap([2.0], seed=1)
    assert single.mean == 2.0 and single.low is None and single.high is None


def test_block_bootstrap_widens_the_interval_of_autocorrelated_savings():
    # A daily cycle: hours inside a day are correlated, so day blocks keep that structure.
    values = [1.0 + (0.8 if hour % 24 < 12 else -0.8) + 0.05 * (hour // 24) for hour in range(168)]
    interval = moving_block_bootstrap(values, seed=11, samples=500)
    assert interval.low is not None and interval.high is not None
    assert interval.low < interval.mean < interval.high


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (0.1, 0.3, ChangeOutcome.REALIZED),
        (-0.3, 0.01, ChangeOutcome.NOT_REALIZED),
        (-0.3, -0.1, ChangeOutcome.NOT_REALIZED),
        (-0.1, 0.3, ChangeOutcome.INCONCLUSIVE),
        (0.0, 0.3, ChangeOutcome.INCONCLUSIVE),
        (None, None, ChangeOutcome.INCONCLUSIVE),
    ],
)
def test_outcome_rule(low, high, expected):
    assert outcome_rule(low, high, epsilon=0.02) is expected


def test_elasticity_is_zero_for_a_fixed_capacity_workload_and_one_for_an_autoscaled_one():
    start = hour_now() - timedelta(days=7)
    hours = [start + timedelta(hours=index) for index in range(168)]
    load = {hour: 100.0 + 80.0 * ((index % 24) / 24) for index, hour in enumerate(hours)}
    fixed = {hour: 5.0 for hour in hours}
    scaled = {hour: 0.05 * load[hour] for hour in hours}
    assert load_elasticity(fixed, load) == pytest.approx(0.0, abs=1e-9)
    assert load_elasticity(scaled, load) == pytest.approx(1.0, abs=1e-6)


def test_counterfactual_scales_only_load_sensitive_quantities():
    start = hour_now() - timedelta(days=14)
    pre_hours = [start + timedelta(hours=index) for index in range(168)]
    post_hours = [hour + timedelta(days=7) for hour in pre_hours]
    pre_load = {hour: 100.0 + (index % 24) for index, hour in enumerate(pre_hours)}
    post_load = {hour + timedelta(days=7): 2 * value for hour, value in pre_load.items()}

    # A fixed-capacity workload: traffic doubles, cost does not move, so the counterfactual must not either
    # (scaling it by the raw load ratio is the BUG-27 inflation).
    fixed = HourOfWeekCounterfactual.fit({hour: 1.0 for hour in pre_hours}, pre_load)
    aligned = align(fixed, {hour: 0.8 for hour in post_hours}, post_load)
    assert len(aligned) == 168
    assert all(item.counterfactual == pytest.approx(1.0) for item in aligned)
    assert all(item.saving == pytest.approx(0.2) for item in aligned)

    # An autoscaled workload: cost follows load, so doubling the load doubles the counterfactual.
    scaled = HourOfWeekCounterfactual.fit({hour: 0.01 * pre_load[hour] for hour in pre_hours}, pre_load)
    aligned = align(scaled, {hour: 0.01 * post_load[hour] for hour in post_hours}, post_load)
    assert all(item.saving == pytest.approx(0.0, abs=1e-9) for item in aligned)


def test_unmatched_hours_are_not_aligned():
    start = hour_now() - timedelta(days=14)
    model = HourOfWeekCounterfactual.fit({start + timedelta(hours=index): 1.0 for index in range(24)})
    post = {start + timedelta(days=7, hours=index): 1.0 for index in range(48)}
    assert len(align(model, post)) == 24
    assert model.predict(start + timedelta(days=8)) is None
