"""A workload present part of each hour is rated per collected hour, not per covered time (MEA-13).

The collection coverage of an hour (the steps collected for the namespace, from
any of its rows) separates "no data" from "not running": steps collected without
a row of the workload count as zero cost and zero replicas, and an hour is only
dropped when collection itself covered less than half of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.impact.capacity import HOURS_PER_YEAR as IMPACT_HOURS_PER_YEAR
from greenkube.models.metrics import CombinedMetric
from greenkube.verify.model import ChangeOutcome, TimeWindow
from greenkube.verify.series import collected_seconds, hourly_buckets, window_hours
from greenkube.verify.strategy import HOURS_PER_YEAR, MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now  # pyrefly: ignore[missing-import]

H0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
STEP = 300


def _row(pod: str, at: datetime, *, cost: float = 0.001, owner: str = "batch") -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="prod",
        owner_kind="Deployment",
        owner_name=owner,
        timestamp=at,
        duration_seconds=STEP,
        total_cost=cost,
        cost_cpu=cost * 0.7,
        cost_ram=cost * 0.3,
        cost_source="opencost",
        usage_source="prometheus",
        cpu_request=500,
        memory_request=512 * 1024**2,
        cpu_usage_millicores=100,
        joules=10.0,
        co2e_grams=1.0,
        grid_intensity=300.0,
    )


def _part_hour(hour: datetime, samples: int, *, cost: float = 0.001) -> list[CombinedMetric]:
    return [_row("batch-1", hour + timedelta(seconds=STEP * index), cost=cost) for index in range(samples)]


def _always_on(window: TimeWindow) -> list[CombinedMetric]:
    rows = []
    moment = window.start
    while moment < window.end:
        rows.append(_row("other-1", moment, owner="other"))
        moment += timedelta(seconds=STEP)
    return rows


def test_sim_e1_a_pod_running_35_minutes_of_a_collected_hour_costs_its_clock_hour_cost():
    """Audit sim E1: 7 samples at 0.001 then absent, the namespace collected all hour -> 0.007/h."""
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=1))
    rows = _part_hour(H0, 7)
    collected = collected_seconds(rows + _always_on(window), window, STEP)
    assert collected == {H0: pytest.approx(3600.0)}
    [bucket] = hourly_buckets(rows, window, STEP, collected=collected)
    assert bucket.cost == pytest.approx(0.007)
    assert bucket.cost_coverage == pytest.approx(1.0)
    assert bucket.coverage == pytest.approx(1.0)
    assert bucket.replicas == pytest.approx(7 / 12)
    assert bucket.co2e_g == pytest.approx(7.0)


def test_a_collected_hour_without_the_workload_counts_as_zero_cost():
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=2))
    rows = _part_hour(H0, 12)
    collected = collected_seconds(rows + _always_on(window), window, STEP)
    first, second = hourly_buckets(rows, window, STEP, collected=collected)
    assert first.cost == pytest.approx(0.012)
    assert second.cost == pytest.approx(0.0)
    assert second.cost_coverage == pytest.approx(1.0)
    assert second.replicas == pytest.approx(0.0)


def test_a_collection_gap_is_still_extrapolated_and_an_uncollected_hour_is_dropped():
    """No row of the namespace at all: the steps were not collected, the covered rate stays."""
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=2))
    rows = _part_hour(H0, 7)
    collected = collected_seconds(rows, window, STEP)
    first, second = hourly_buckets(rows, window, STEP, collected=collected)
    assert first.cost == pytest.approx(0.012)
    assert first.coverage == pytest.approx(7 / 12)
    assert second.cost is None and second.coverage == 0.0


def test_one_annualisation_constant_is_shared_with_the_impact_model():
    assert HOURS_PER_YEAR == IMPACT_HOURS_PER_YEAR == 8760.0


def test_a_part_time_workload_with_a_50_percent_cut_is_annualised_on_its_presence():
    """Present 35 min/h, 0.001 -> 0.0005 per sample: the saving is 0.0035 per clock hour."""
    strategy = RequestRightsizingStrategy()
    settings = MeasurementSettings(bootstrap_samples=200)
    change = change_event(hour_now() - timedelta(days=9))
    plan = strategy.required_windows(change, settings)

    def buckets(window: TimeWindow, cost: float):
        rows = []
        for hour in window_hours(window):
            rows.extend(_part_hour(hour, 7, cost=cost))
        for row in rows:
            row.owner_name = "api"
            # The request follows the cost: a 50% cut of both.
            row.cpu_request = int(500 * cost / 0.001)
            row.memory_request = int(512 * 1024**2 * cost / 0.001)
            row.cpu_usage_millicores = 50
        collected = collected_seconds(rows + _always_on(window), window, STEP)
        return hourly_buckets(rows, window, STEP, collected=collected)

    result = strategy.estimate(
        change,
        plan,
        buckets(plan.pre, 0.001),
        buckets(plan.post, 0.0005),
        load=None,
        confounders=[],
        projections={"allocated_cost": None},
        settings=settings,
        seed_parts=(change.change_id, 1),
    )
    cost = {item.quantity: item for item in result.estimates}["allocated_cost"]
    assert cost.annualized == pytest.approx(0.0035 * HOURS_PER_YEAR, rel=1e-6)
    assert result.outcome is ChangeOutcome.REALIZED
