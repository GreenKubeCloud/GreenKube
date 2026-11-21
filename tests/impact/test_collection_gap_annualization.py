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
    return projector.container_resources(ref, target, cpu_millicores=(1000, 100)), projector.workload_deletion(
        ref, target
    )


def test_a_cluster_wide_collection_gap_does_not_lower_the_projection():
    end = _hour()
    full_cut, full_delete = _project(_rows("api", end, 24), "api", end)
    gap_rows = _rows("api", end, 24, skip=lambda index: 6 <= index < 18) + _rows(
        "other", end, 24, skip=lambda index: 6 <= index < 18
    )
    gap_cut, gap_delete = _project(gap_rows, "api", end)

    assert gap_cut.allocated_cost_annual == pytest.approx(full_cut.allocated_cost_annual, rel=0.01)
    assert gap_delete.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR, rel=0.01)
    assert full_delete.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR, rel=0.01)
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
    _, delete = _project(rows, "cron", end, lookback=days * DAY)
    assert delete.capacity_released.replica_hours == pytest.approx(HOURS_PER_YEAR / 24, rel=0.01)


def test_a_young_target_counts_from_its_first_sample_not_the_hour_floor():
    """First sampled at hh:35, scanned at hh+1:30: rollup rows and a summary, as a bounded scan reads them."""
    first_hour = _hour() - timedelta(hours=2)
    end = first_hour + timedelta(hours=1, minutes=30)
    hours = [
        WorkloadUsageHour(
            hour_bucket=first_hour,
            namespace="apps",
            target_kind="Deployment",
            target_name="young",
            step_count=5,
        ),
        WorkloadUsageHour(
            hour_bucket=first_hour + timedelta(hours=1),
            namespace="apps",
            target_kind="Deployment",
            target_name="young",
            step_count=6,
        ),
        WorkloadUsageHour(
            hour_bucket=first_hour,
            namespace="apps",
            target_kind="Deployment",
            target_name="other",
            step_count=12,
        ),
        WorkloadUsageHour(
            hour_bucket=first_hour + timedelta(hours=1),
            namespace="apps",
            target_kind="Deployment",
            target_name="other",
            step_count=6,
        ),
    ]
    snapshot: UsageSnapshot = summarize(hours, source="rows")
    summary: WorkloadUsageSummary = snapshot.summaries[("apps", "Deployment", "young")][""]
    assert summary.first_hour_steps == 5
    assert snapshot.collected_steps_by_hour == {first_hour: 12, first_hour + timedelta(hours=1): 6}
    row = BaselineMetric.model_construct(
        pod_name="young",
        namespace="apps",
        owner_kind="Deployment",
        owner_name="young",
        node="n1",
        timestamp=first_hour + timedelta(hours=1),
        sample_count=11,
        duration_seconds=11 * 300,
        cpu_request=1000,
        memory_request=GIB,
        cpu_usage_millicores=50,
        memory_usage_bytes=GIB // 4,
        total_cost=0.0025 * 11,
        cost_cpu=0.0025 * 11,
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
    delta = projector.workload_deletion(
        projector.workload_ref("apps", "Deployment", "young"), context.workload_rows(("apps", "Deployment", "young"))
    )
    assert delta.assumptions["coverage_hours"] == pytest.approx(55 / 60, rel=0.02)
    assert delta.allocated_cost_annual == pytest.approx(0.03 * HOURS_PER_YEAR, rel=0.02)
