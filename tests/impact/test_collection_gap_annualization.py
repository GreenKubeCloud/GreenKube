# tests/impact/test_collection_gap_annualization.py
"""Projections annualize over the hours the collector observed, not the wall-clock span (IMP-04).

Hours in which the collector produced no row for any target (controller down,
Prometheus outage) stay out of the denominator; a CronJob keeps its duty cycle
because other targets were collected while it did not run; and the first hour
of a target counts from its first sample, not from the hour floor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.workload_usage import summarize
from greenkube.impact import ImpactProjector
from greenkube.impact.types import HOURS_PER_YEAR
from greenkube.models.metrics import CombinedMetric
from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot, WorkloadUsageHour, WorkloadUsageSummary

GIB = 1024**3
DAY = 24 * 3600.0


def _hour() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _row(name: str, at: datetime) -> CombinedMetric:
    return CombinedMetric(
        pod_name=f"{name}-7f9c5d9f6d-a1b2c",
        namespace="apps",
        owner_kind="Deployment",
        owner_name=name,
        node="n1",
        cpu_request=1000,
        memory_request=GIB,
        cpu_usage_millicores=50,
        memory_usage_bytes=GIB // 4,
        total_cost=0.0025,
        cost_cpu=0.0025,
        cost_ram=0.0,
        timestamp=at,
        duration_seconds=300,
    )


def _rows(name: str, end: datetime, hours: int, *, skip=lambda hour: False) -> list[CombinedMetric]:
    rows = []
    for hour_index in range(hours):
        hour = end - timedelta(hours=hours - hour_index)
        if skip(hour_index):
            continue
        rows.extend(_row(name, hour + timedelta(minutes=5 * step)) for step in range(12))
    return rows


def _project(rows, name: str, end: datetime, lookback: float = DAY):
    projector = ImpactProjector(Config(), rows=rows, analysis_window_seconds=lookback, window_end=end)
    ref = projector.workload_ref("apps", "Deployment", name)
    target = [row for row in rows if row.owner_name == name]
    return projector.container_resources(ref, target, cpu_millicores=(1000, 100))


def test_a_cluster_wide_collection_gap_does_not_lower_the_projection():
    end = _hour()
    full_cut = _project(_rows("api", end, 24), "api", end)
    gap_rows = _rows("api", end, 24, skip=lambda index: 6 <= index < 18) + _rows(
        "other", end, 24, skip=lambda index: 6 <= index < 18
    )
    gap_cut = _project(gap_rows, "api", end)

    assert gap_cut.allocated_cost_annual == pytest.approx(full_cut.allocated_cost_annual, rel=0.01)
    assert gap_cut.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR, rel=0.01)
    assert full_cut.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR, rel=0.01)
    assert gap_cut.assumptions["collection_gap_hours"] == pytest.approx(12.0)
    assert gap_cut.assumptions["collected_hours"] == pytest.approx(12.0)
    assert full_cut.assumptions["collection_gap_hours"] == pytest.approx(0.0)
    # Less than 90% of the coverage collected: the projection is less certain.
    assert gap_cut.confidence < full_cut.confidence


def test_a_cronjob_keeps_its_duty_cycle_while_other_targets_were_collected():
    end = _hour()
    days = 3
    cron = [row for row in _rows("cron", end, 24 * days, skip=lambda index: index % 24 != 0)]
    rows = cron + _rows("other", end, 24 * days)
    cut = _project(rows, "cron", end, lookback=days * DAY)
    assert cut.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR / 24, rel=0.01)


def _hour_row(hour, name, steps, last_offset=None) -> WorkloadUsageHour:
    return WorkloadUsageHour(
        hour_bucket=hour,
        namespace="apps",
        target_kind="Deployment",
        target_name=name,
        step_count=steps,
        latest_sample_offset_seconds=last_offset,
    )


def _young_projection(hours, end, samples):
    snapshot: UsageSnapshot = summarize(hours, source="rows")
    row = BaselineMetric.model_construct(
        pod_name="young",
        namespace="apps",
        owner_kind="Deployment",
        owner_name="young",
        node="n1",
        timestamp=end.replace(minute=0),
        sample_count=samples,
        duration_seconds=samples * 300,
        cpu_request=1000,
        memory_request=GIB,
        cpu_usage_millicores=50,
        memory_usage_bytes=GIB // 4,
        total_cost=0.0025 * samples,
        cost_cpu=0.0025 * samples,
        cost_ram=0.0,
        estimation_reasons=[],
    )
    context = OptimizationContext(
        config=Config(),
        metrics=[row],
        analysis_window_seconds=DAY,
        window_start=end - timedelta(seconds=DAY),
        window_end=end,
        usage=snapshot,
    )
    projector = ImpactProjector.for_context(context)
    delta = projector.container_resources(
        projector.workload_ref("apps", "Deployment", "young"),
        context.container_rows(("apps", "Deployment", "young"), None),
        cpu_millicores=(1000, 100),
    )
    return snapshot, delta


def test_a_young_target_counts_from_its_first_sample_not_the_hour_floor():
    """First sampled at hh:35 (last sample hh:55), scanned at hh+1:30: rollup rows and a summary."""
    first_hour = _hour() - timedelta(hours=2)
    end = first_hour + timedelta(hours=1, minutes=30)
    hours = [
        _hour_row(first_hour, "young", 5, last_offset=3300),
        _hour_row(first_hour + timedelta(hours=1), "young", 6, last_offset=1500),
        _hour_row(first_hour, "other", 12, last_offset=3300),
        _hour_row(first_hour + timedelta(hours=1), "other", 6, last_offset=1500),
    ]
    snapshot, delta = _young_projection(hours, end, 11)
    summary: WorkloadUsageSummary = snapshot.summaries[("apps", "Deployment", "young")][""]
    assert summary.first_hour_steps == 5
    assert summary.first_hour_last_offset == 3300
    assert snapshot.collected_steps_by_hour == {first_hour: 12, first_hour + timedelta(hours=1): 6}
    assert delta.assumptions["coverage_hours"] == pytest.approx(55 / 60, rel=0.02)
    assert delta.assumptions["annualization_factor"] == pytest.approx(HOURS_PER_YEAR * 60 / 55, rel=0.02)


def test_a_gap_inside_the_first_hour_does_not_move_the_first_observation():
    """Ran 10:00-10:10, then 11:00-12:00: the first hour's samples are at its start, not its end."""
    first_hour = _hour() - timedelta(hours=3)
    end = first_hour + timedelta(hours=2)
    hours = [
        _hour_row(first_hour, "young", 2, last_offset=300),
        _hour_row(first_hour + timedelta(hours=1), "young", 12, last_offset=3300),
        _hour_row(first_hour, "other", 12, last_offset=3300),
        _hour_row(first_hour + timedelta(hours=1), "other", 12, last_offset=3300),
    ]
    _, delta = _young_projection(hours, end, 14)
    # 14 samples over two collected hours, not over 70 minutes (1.71x too high).
    assert delta.assumptions["coverage_hours"] == pytest.approx(2.0, rel=0.01)
    assert delta.assumptions["annualization_factor"] == pytest.approx(HOURS_PER_YEAR / 2.0, rel=0.01)
