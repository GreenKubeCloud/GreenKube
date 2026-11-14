"""RequestRightsizingStrategy outcomes (C3, C4), including the audit scenarios as regressions."""

from datetime import timedelta

import pytest

from greenkube.verify.load_driver import LoadDriverResolver
from greenkube.verify.model import ChangeOutcome, Confounder
from greenkube.verify.series import hourly_buckets
from greenkube.verify.strategy import HOURS_PER_YEAR, MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now, workload_rows  # pyrefly: ignore[missing-import]

STEP = 300
SETTINGS = MeasurementSettings(bootstrap_samples=300)
STRATEGY = RequestRightsizingStrategy()


def _measure(pre_rows, post_rows, *, change=None, load=None, confounders=(), projected=None):
    change = change or change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    pre = hourly_buckets(pre_rows(plan.pre), plan.pre, STEP)
    post = hourly_buckets(post_rows(plan.post), plan.post, STEP)
    result = STRATEGY.estimate(
        change,
        plan,
        pre,
        post,
        load=load,
        confounders=list(confounders),
        projections={"allocated_cost": projected},
        settings=SETTINGS,
        seed_parts=(change.change_id, 1),
    )
    return result, {item.quantity: item for item in result.estimates}


def test_windows_follow_the_merge_and_the_rollout():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed, decision=completed - timedelta(hours=3))
    plan = STRATEGY.required_windows(change, SETTINGS)
    assert plan.pre.end == completed - timedelta(hours=3)
    assert plan.pre.duration == timedelta(days=7)
    assert plan.post.start == completed + timedelta(hours=1)
    assert plan.post.duration == timedelta(days=7)
    assert plan.pre.end <= plan.post.start


def test_verify_e2e_cpu_cut_is_realized_on_cost_and_claims_no_unchanged_co2e():
    """The audit's verify_e2e scenario: 2 replicas, 1000m -> 300m, cost -20%, CO2e unchanged."""

    def before(window):
        return workload_rows(window, cost=0.01, step=timedelta(minutes=5), components=False)

    def after(window):
        return workload_rows(window, cost=0.008, cpu_request=300, step=timedelta(minutes=5), components=False)

    result, estimates = _measure(before, after, projected=1500.0)
    assert result.outcome is ChangeOutcome.REALIZED
    cost = estimates["allocated_cost"]
    true_saving = 0.002 * 12 * 2 * HOURS_PER_YEAR  # 0.024 per pod-hour, 2 replicas
    assert cost.annualized == pytest.approx(true_saving, rel=1e-6)
    assert cost.ci_low is not None and cost.ci_low > 0
    # Projection accuracy is reported, not a gate: 28% of the projection is still realized.
    assert cost.projection_accuracy == pytest.approx(true_saving / 1500.0)
    co2e = estimates["co2e_g"]
    # Operational CO2e is claimable (allocated by the Impact Model), but it did not move here.
    assert co2e.rate_per_hour == pytest.approx(0.0, abs=1e-9)
    assert co2e.outcome is not ChangeOutcome.REALIZED
    assert estimates["core_hours"].rate_per_hour == pytest.approx(1.4)


def test_traffic_growth_does_not_inflate_a_fixed_capacity_saving():
    """BUG-27: a 4x traffic ratio must not scale the counterfactual of a workload whose cost ignores load."""
    from greenkube.verify.load_driver import ResolvedLoad
    from greenkube.verify.model import LoadDriverInfo

    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)

    def series(window, factor):
        values = {}
        moment = window.start.replace(minute=0)
        while moment < window.end:
            values[moment] = factor * (100.0 + (moment.hour % 12) * 10)
            moment += timedelta(hours=1)
        return values

    load = ResolvedLoad(
        info=LoadDriverInfo(key="istio_rps", label="Istio", coverage_pre=1.0, coverage_post=1.0),
        pre=series(plan.pre, 1.0),
        post=series(plan.post, 4.0),
    )
    result, estimates = _measure(
        lambda window: workload_rows(window),
        lambda window: workload_rows(window, cpu_request=300),
        change=change,
        load=load,
    )
    cost = estimates["allocated_cost"]
    assert cost.elasticity == pytest.approx(0.0, abs=1e-9)
    assert cost.rate_per_hour == pytest.approx(2 * 0.7 * 0.08, rel=1e-6)
    assert result.outcome is ChangeOutcome.REALIZED


def test_no_change_is_not_realized():
    result, estimates = _measure(lambda window: workload_rows(window), lambda window: workload_rows(window))
    assert result.outcome is ChangeOutcome.NOT_REALIZED
    assert estimates["allocated_cost"].rate_per_hour == pytest.approx(0.0, abs=1e-12)


def test_a_cost_increase_is_reported_signed():
    result, estimates = _measure(
        lambda window: workload_rows(window), lambda window: workload_rows(window, cpu_request=1500)
    )
    assert result.outcome is ChangeOutcome.NOT_REALIZED
    cost = estimates["allocated_cost"]
    assert cost.annualized is not None and cost.annualized < 0
    assert cost.ci_high is not None and cost.ci_high < 0


def test_a_small_saving_in_noise_is_inconclusive():
    result, estimates = _measure(
        lambda window: workload_rows(window, noise=0.6, seed=1),
        lambda window: workload_rows(window, cpu_request=950, noise=0.6, seed=2),
    )
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    cost = estimates["allocated_cost"]
    assert cost.ci_low is not None and cost.ci_low < 0 < (cost.ci_high or 0)


def test_low_post_window_coverage_is_inconclusive():
    def partial(window):
        return workload_rows(window.model_copy(update={"end": window.start + timedelta(days=3)}), cpu_request=300)

    result, estimates = _measure(lambda window: workload_rows(window), partial)
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert estimates["allocated_cost"].coverage < 0.8
    assert "coverage" in result.reasons[0]


def test_untrusted_cost_is_inconclusive():
    result, estimates = _measure(
        lambda window: workload_rows(window, is_estimated=True),
        lambda window: workload_rows(window, cpu_request=300, is_estimated=True),
    )
    assert result.outcome is ChangeOutcome.INCONCLUSIVE
    assert "no trusted allocated cost" in estimates["allocated_cost"].reason
    # Freed capacity is still measured and reported.
    assert estimates["core_hours"].rate_per_hour == pytest.approx(1.4)


def test_a_severe_confounder_contaminates_the_measurement():
    finding = Confounder(kind="overlapping_change", severity="severe", detail="another change landed")
    result, _ = _measure(
        lambda window: workload_rows(window),
        lambda window: workload_rows(window, cpu_request=300),
        confounders=[finding],
    )
    assert result.outcome is ChangeOutcome.CONTAMINATED
    assert result.reasons == ["another change landed"]


def test_cpu_usage_load_driver_is_resolved_when_nothing_else_is_available():
    change = change_event(hour_now() - timedelta(days=9))
    plan = STRATEGY.required_windows(change, SETTINGS)
    pre = hourly_buckets(workload_rows(plan.pre), plan.pre, STEP)
    post = hourly_buckets(workload_rows(plan.post), plan.post, STEP)
    resolver = LoadDriverResolver()
    from greenkube.verify.series import quantity_series

    resolved = resolver.resolve(
        change,
        plan.pre,
        plan.post,
        {"cpu_usage": quantity_series(pre, "cpu_usage_cores")},
        {"cpu_usage": quantity_series(post, "cpu_usage_cores")},
    )
    assert resolved is not None and resolved.info.weak


def test_an_unrelated_replica_drop_is_not_credited_to_a_request_change():
    """4 -> 2 replicas during the post-window with unchanged requests: no saving (it was credited before 2.1)."""

    def before(window):
        return workload_rows(window, pods=("a", "b", "c", "d"), cost=0.01, step=timedelta(minutes=5))

    def after(window):
        return workload_rows(window, pods=("a", "b"), cost=0.01, step=timedelta(minutes=5))

    result, estimates = _measure(before, after, projected=500.0)
    cost = estimates["allocated_cost"]
    assert result.outcome is not ChangeOutcome.REALIZED
    assert cost.annualized == pytest.approx(0.0, abs=1e-6)
    assert cost.counterfactual_per_hour == pytest.approx(cost.actual_per_hour)


def test_a_request_cut_with_fewer_replicas_credits_the_per_replica_saving_times_replica_hours():
    """1000m -> 300m (per pod-hour $0.12 -> $0.05) while replicas drop 4 -> 2: saving = $0.07 x 2 replicas."""

    def before(window):
        return workload_rows(window, pods=("a", "b", "c", "d"), cost=0.01, step=timedelta(minutes=5))

    def after(window):
        return workload_rows(window, pods=("a", "b"), cost=0.01 * 5 / 12, cpu_request=300, step=timedelta(minutes=5))

    result, estimates = _measure(before, after, projected=1500.0)
    cost = estimates["allocated_cost"]
    assert result.outcome is ChangeOutcome.REALIZED
    assert cost.rate_per_hour == pytest.approx((0.12 - 0.05) * 2, rel=1e-6)
    # Freed capacity: 0.7 cores per replica x 2 replicas.
    assert estimates["core_hours"].rate_per_hour == pytest.approx(1.4)
