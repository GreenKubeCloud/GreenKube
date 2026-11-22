"""Accelerated demo windows can decide, and inconclusive reasons name the true cause (MEA-10).

The live accelerated changes (6-hour baseline, 72-minute post-window, 30 s
collection step) were inconclusive by construction: no post hour can match a
baseline hour of the week, and the stored reason said "no trusted allocated
cost" although the baseline hour was dropped for its 49% coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.verify.model import ChangeEvent, ChangeOutcome, HourBucket, WorkloadRef
from greenkube.verify.series import hourly_buckets
from greenkube.verify.strategy import (
    ACCELERATED_MODEL,
    ACCELERATED_SLOT_SECONDS,
    MeasurementSettings,
    RequestRightsizingStrategy,
)
from tests.verify.fixtures import workload_rows  # pyrefly: ignore[missing-import]

UTC = timezone.utc
COMPLETED = datetime(2026, 11, 9, 14, 7, 30, tzinfo=UTC)
STEP = timedelta(seconds=30)
#: The quickstart overlay: 6 hours of baseline, 72 minutes after a 3-minute warm-up.
DEMO = MeasurementSettings(
    pre_window_days=0.25, post_window_days=0.05, warmup_hours=0.05, bootstrap_samples=300, accelerated=True
)


def _change() -> ChangeEvent:
    return ChangeEvent(
        change_id="demo",
        target=WorkloadRef(namespace="prod", kind="Deployment", name="api"),
        rollout_started_at=COMPLETED - timedelta(minutes=2),
        rollout_completed_at=COMPLETED,
        baseline_window_end=COMPLETED - timedelta(minutes=20),
    )


def _estimate(settings: MeasurementSettings, slot_seconds: int):
    change = _change()
    strategy = RequestRightsizingStrategy()
    plan = strategy.required_windows(change, settings)
    pre_rows = workload_rows(plan.pre, step=STEP, cpu_request=1000, noise=0.03, seed=1)
    post_rows = workload_rows(plan.post, step=STEP, cpu_request=300, noise=0.03, seed=2)
    pre = hourly_buckets(pre_rows, plan.pre, 30, slot_seconds=slot_seconds)
    post = hourly_buckets(post_rows, plan.post, 30, slot_seconds=slot_seconds)
    return strategy.estimate(
        change,
        plan,
        pre,
        post,
        load=None,
        confounders=[],
        projections={},
        settings=settings,
        seed_parts=("demo",),
    )


def test_accelerated_windows_reach_a_decided_verdict():
    """Two pods cut from 1000m to 300m at 0.08/core-hour: 0.112/h saved (0.20/h -> 0.088/h)."""
    result = _estimate(DEMO, ACCELERATED_SLOT_SECONDS)
    cost = {item.quantity: item for item in result.estimates}["allocated_cost"]
    assert result.outcome is ChangeOutcome.REALIZED
    assert cost.rate_per_hour == pytest.approx(0.112, rel=0.05)
    assert cost.method == ACCELERATED_MODEL
    params = result.counterfactual_params
    assert params["model"] == ACCELERATED_MODEL
    assert params["representative"] is False
    assert params["time_grid_seconds"] == ACCELERATED_SLOT_SECONDS
    assert params["demo_thresholds"]["min_post_slots"] >= 6


def test_the_same_windows_without_acceleration_stay_inconclusive_for_the_true_reason():
    settings = MeasurementSettings(pre_window_days=0.25, post_window_days=0.05, warmup_hours=0.05)
    result = _estimate(settings, 3600)
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert result.counterfactual_params.get("representative", True) is True
    # The only clock hour starting in the 72-minute post-window (15:00) is 38% covered: that is the cause.
    assert result.reasons == [
        "1 hour of allocated cost in the post-change window, each below the 50% coverage an hour needs (best 38%)"
    ]


def _reason(pre, post, quantity="allocated_cost"):
    change = _change().model_copy(update={"baseline_window_end": COMPLETED, "rollout_completed_at": COMPLETED})
    strategy = RequestRightsizingStrategy()
    settings = MeasurementSettings()
    plan = strategy.required_windows(change, settings)
    result = strategy.estimate(
        change, plan, pre, post, load=None, confounders=[], projections={}, settings=settings, seed_parts=("r",)
    )
    return {item.quantity: item for item in result.estimates}[quantity].reason or ""


HOUR = datetime(2026, 11, 9, 9, tzinfo=UTC)
POST = [
    HourBucket(
        hour=COMPLETED.replace(minute=0, second=0) + timedelta(hours=2 + index),
        coverage=1.0,
        replicas=1.0,
        core_hours=0.3,
        gib_hours=1.0,
        cost=0.01,
        cost_cpu=0.007,
        cost_ram=0.003,
        cost_coverage=1.0,
    )
    for index in range(3)
]


def test_an_hour_dropped_for_coverage_is_named_as_such():
    """minikube 0335c712: 59 samples x 30 s = 49% of the hour, trusted cost -> not 'no trusted cost'."""
    pre = [
        HourBucket(
            hour=HOUR,
            coverage=0.4917,
            replicas=1.0,
            core_hours=0.0864,
            gib_hours=1.0,
            cost=0.0033,
            cost_cpu=0.003,
            cost_ram=0.0003,
            cost_coverage=0.4917,
        )
    ]
    reason = _reason(pre, POST)
    assert "no trusted" not in reason
    assert "below the 50% coverage" in reason and "best 49%" in reason and "baseline" in reason
    request = _reason(pre, POST, "core_hours")
    assert "trusted" not in request and "below the 50% coverage" in request


def test_rows_without_trusted_cost_are_named_as_such():
    pre = [HourBucket(hour=HOUR, coverage=1.0, replicas=1.0, core_hours=1.0, gib_hours=1.0, cost_coverage=0.0)]
    reason = _reason(pre, POST)
    assert "no trusted allocated cost" in reason and "metric rows" in reason


def test_a_window_without_rows_is_named_as_such():
    reason = _reason([HourBucket(hour=HOUR)], POST)
    assert "no metric rows" in reason and "baseline" in reason
