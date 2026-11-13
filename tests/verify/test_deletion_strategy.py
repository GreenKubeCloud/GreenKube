"""``ResourceDeletionStrategy`` (roadmap H6): a deletion realizes the released allocated rate."""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.verify.deletion import METHOD_ALLOCATED_RATE, METHOD_PRE_WINDOW, ResourceDeletionStrategy
from greenkube.verify.model import ChangeOutcome, Confounder, HourBucket
from greenkube.verify.strategy import HOURS_PER_YEAR, MeasurementSettings, strategy_for
from tests.verify.fixtures import change_event, hour_now  # pyrefly: ignore[missing-import]

SETTINGS = MeasurementSettings()
STRATEGY = ResourceDeletionStrategy()


def _estimate(pre=(), post=(), *, projected=None, confounders=()):
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    return STRATEGY.estimate(
        change,
        plan,
        list(pre),
        list(post),
        load=None,
        confounders=list(confounders),
        projections={"allocated_cost": projected},
        settings=SETTINGS,
        seed_parts=(change.change_id, 1),
    )


def test_the_strategy_is_registered_for_the_delete_resource_kind() -> None:
    assert isinstance(strategy_for("delete_resource"), ResourceDeletionStrategy)


def test_an_absent_resource_realizes_its_allocated_rate_at_decision() -> None:
    result = _estimate(projected=219.15)

    (estimate,) = result.estimates
    assert result.outcome is ChangeOutcome.REALIZED
    assert estimate.method == METHOD_ALLOCATED_RATE
    assert estimate.annualized == pytest.approx(219.15)
    assert estimate.actual_per_hour == 0.0
    assert estimate.projection_accuracy == pytest.approx(1.0)


def test_the_cost_observed_before_the_change_wins_over_the_projection() -> None:
    start = hour_now() - timedelta(days=12)
    pre = [
        HourBucket(hour=start + timedelta(hours=index), coverage=1.0, cost=0.05, cost_coverage=1.0)
        for index in range(10)
    ]
    result = _estimate(pre, projected=100.0)

    (estimate,) = result.estimates
    assert estimate.method == METHOD_PRE_WINDOW
    assert estimate.rate_per_hour == pytest.approx(0.05)
    assert estimate.annualized == pytest.approx(0.05 * HOURS_PER_YEAR)


def test_a_resource_still_observed_after_the_change_is_not_realized() -> None:
    post = [HourBucket(hour=hour_now() - timedelta(days=1), coverage=0.5, cost=0.05, cost_coverage=1.0)]
    result = _estimate(post=post, projected=100.0)
    assert result.outcome is ChangeOutcome.NOT_REALIZED
    assert "still observed" in result.reasons[0]


def test_no_rate_is_inconclusive_and_a_severe_confounder_contaminates() -> None:
    assert _estimate().outcome is ChangeOutcome.INCONCLUSIVE
    severe = Confounder(kind="node_pool_change", severity="severe", detail="the node pool was replaced")
    contaminated = _estimate(projected=50.0, confounders=[severe])
    assert contaminated.outcome is ChangeOutcome.CONTAMINATED
    assert contaminated.reasons == ["the node pool was replaced"]
