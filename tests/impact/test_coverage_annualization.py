# tests/impact/test_coverage_annualization.py
"""Projections annualize over the target's observed coverage (Impact Model 2.0.0).

E2E defect: a workload observed for two hours inside a 14-day lookback was
annualized as if those two hours were spread over fourteen days, a 168x
underestimate (``window_hours`` was the lookback, ``projector.py`` baseline
and ``model.py`` ``_finalize``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.impact import ImpactProjector, ReservationAwareImpactModel, WorkloadBaseline, WorkloadDeletion
from greenkube.impact.model import (
    MIN_COVERAGE_HOURS,
    SHORT_COVERAGE_CONFIDENCE_FLOOR,
    SHORT_COVERAGE_HOURS,
    annualization,
)
from greenkube.impact.projector import first_observed_from_summaries
from greenkube.impact.types import HOURS_PER_YEAR, ObservedCoverage, WorkloadRef
from greenkube.models.metrics import CombinedMetric
from greenkube.models.workload_usage import BaselineMetric, UsageSnapshot, WorkloadUsageSummary

GIB = 1024**3
LOOKBACK = 14 * 24 * 3600.0
STEP_COST = 0.004  # per 5-minute sample: 0.048 per hour


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def _rows(end: datetime, hours: float, name: str = "young") -> list[CombinedMetric]:
    count = int(hours * 12)
    return [
        CombinedMetric(
            pod_name=f"{name}-7f9c5d9f6d-a1b2c",
            namespace="apps",
            owner_kind="Deployment",
            owner_name=name,
            node="n1",
            cpu_request=500,
            memory_request=GIB,
            cpu_usage_millicores=100,
            memory_usage_bytes=GIB // 4,
            total_cost=STEP_COST,
            timestamp=end - timedelta(minutes=5 * (count - index)),
            duration_seconds=300,
        )
        for index in range(count)
    ]


def _deletion(projector: ImpactProjector, rows, name: str = "young"):
    return projector.workload_deletion(projector.workload_ref("apps", "Deployment", name), rows)


class TestTwoHourWorkloadInFourteenDayLookback:
    def test_raw_rows_annualize_over_two_observed_hours(self):
        now = _now()
        rows = _rows(now, 2.0)
        projector = ImpactProjector(Config(), rows=rows, analysis_window_seconds=LOOKBACK, window_end=now)
        delta = _deletion(projector, rows)

        observed_cost = STEP_COST * 24
        # Before the fix: observed_cost * 8760 / 336 = 0.27; the run-rate is 0.048/h.
        assert delta.allocated_cost_annual == pytest.approx(observed_cost * HOURS_PER_YEAR / 2.0)
        assert delta.allocated_cost_annual == pytest.approx(0.048 * HOURS_PER_YEAR)
        assumptions = delta.assumptions
        assert assumptions["window_hours"] == pytest.approx(2.0)
        assert assumptions["lookback_hours"] == pytest.approx(336.0)
        assert assumptions["coverage_hours"] == pytest.approx(2.0)
        assert assumptions["coverage_basis"] == "samples"
        assert assumptions["short_coverage"] is True
        assert any("2.0 h of observation" in note for note in assumptions["notes"])
        # Short coverage lowers the confidence, never below the floor.
        full = ImpactProjector(Config(), rows=rows, analysis_window_seconds=LOOKBACK)
        baseline = full.baseline(full.workload_ref("apps", "Deployment", "young"), rows)
        unscaled = ReservationAwareImpactModel().project(
            WorkloadBaseline(ref=baseline.ref, samples=baseline.samples, window_hours=2.0),
            WorkloadDeletion(),
            full.pool,
        )
        assert delta.confidence == pytest.approx(unscaled.confidence * SHORT_COVERAGE_CONFIDENCE_FLOOR, abs=1e-4)

    def test_young_and_old_workloads_with_the_same_run_rate_project_the_same(self):
        now = _now()
        young = _rows(now, 2.0, "young")
        old = _rows(now, 14 * 24, "old")
        projector = ImpactProjector(Config(), rows=young + old, analysis_window_seconds=LOOKBACK, window_end=now)
        young_delta = _deletion(projector, young, "young")
        old_delta = _deletion(projector, old, "old")
        assert young_delta.allocated_cost_annual == pytest.approx(old_delta.allocated_cost_annual)
        assert young_delta.co2e_allocated_annual_g == pytest.approx(old_delta.co2e_allocated_annual_g)
        assert old_delta.assumptions["coverage_hours"] == pytest.approx(336.0)
        assert "short_coverage" not in old_delta.assumptions
        assert "notes" not in old_delta.assumptions

    def test_context_uses_the_target_summary_and_rollup_baseline_rows(self):
        """Bounded scans: baseline rows are aggregated, so coverage comes from the summary."""
        now = _now()
        first_hour = now.replace(minute=0) - timedelta(hours=2)
        row = BaselineMetric.model_construct(
            pod_name="young",
            namespace="apps",
            owner_kind="Deployment",
            owner_name="young",
            node="n1",
            timestamp=first_hour + timedelta(hours=1),
            sample_count=24,
            duration_seconds=7200,
            cpu_request=500,
            memory_request=GIB,
            cpu_usage_millicores=100,
            memory_usage_bytes=GIB // 4,
            total_cost=STEP_COST * 24,
            pue=1.2,
            grid_intensity=50.0,
            estimation_reasons=[],
        )
        snapshot = UsageSnapshot(source="rollup")
        snapshot.summaries[("apps", "Deployment", "young")] = {
            "": WorkloadUsageSummary(
                namespace="apps",
                target_kind="Deployment",
                target_name="young",
                first_hour=first_hour,
                last_hour=first_hour + timedelta(hours=1),
            )
        }
        context = OptimizationContext(
            config=Config(),
            metrics=[row],
            analysis_window_seconds=LOOKBACK,
            window_start=now - timedelta(seconds=LOOKBACK),
            window_end=now,
            usage=snapshot,
        )
        projector = ImpactProjector.for_context(context)
        delta = _deletion(projector, context.workload_rows(("apps", "Deployment", "young")))
        coverage = (now - first_hour).total_seconds() / 3600.0
        assert delta.assumptions["coverage_basis"] == "target_summary"
        assert delta.assumptions["coverage_hours"] == pytest.approx(coverage)
        assert delta.allocated_cost_annual == pytest.approx(STEP_COST * 24 * HOURS_PER_YEAR / coverage)

    def test_rollup_rows_without_a_summary_keep_the_lookback(self):
        """An aggregated row's timestamp is not a sample time: never infer coverage from it."""
        now = _now()
        row = BaselineMetric.model_construct(
            pod_name="svc",
            namespace="apps",
            owner_kind="Deployment",
            owner_name="svc",
            node="n1",
            timestamp=now - timedelta(hours=1),
            sample_count=4032,
            duration_seconds=336 * 3600,
            cpu_request=500,
            memory_request=GIB,
            total_cost=1.0,
            estimation_reasons=[],
        )
        projector = ImpactProjector(Config(), rows=[row], analysis_window_seconds=LOOKBACK, window_end=now)
        delta = projector.workload_deletion(projector.workload_ref("apps", "Deployment", "svc"), [row])
        assert delta.assumptions["coverage_basis"] == "lookback"
        assert delta.assumptions["window_hours"] == pytest.approx(336.0)
        assert delta.allocated_cost_annual == pytest.approx(HOURS_PER_YEAR / 336.0)


class TestMinimumCoverage:
    def test_a_few_minutes_are_annualized_over_the_minimum_coverage(self):
        now = _now()
        rows = _rows(now, 1 / 6)  # two 5-minute samples
        projector = ImpactProjector(Config(), rows=rows, analysis_window_seconds=LOOKBACK, window_end=now)
        delta = _deletion(projector, rows)
        assert delta.assumptions["coverage_hours"] == pytest.approx(1 / 6)
        assert delta.assumptions["minimum_coverage_applied"] is True
        assert delta.assumptions["window_hours"] == MIN_COVERAGE_HOURS
        assert delta.allocated_cost_annual == pytest.approx(2 * STEP_COST * HOURS_PER_YEAR / MIN_COVERAGE_HOURS)

    def test_annualization_rules(self):
        ref = WorkloadRef("", "ns", "Deployment", "x")

        def rule(observed: float, lookback: float = 336.0):
            coverage = ObservedCoverage(lookback_hours=lookback, observed_hours=observed, basis="samples")
            return annualization(WorkloadBaseline(ref=ref, samples=(), window_hours=lookback, coverage=coverage))

        assert rule(500.0).hours == 336.0  # never more than the lookback
        assert rule(336.0).confidence_scale == 1.0
        assert rule(84.0).confidence_scale == pytest.approx(84.0 / SHORT_COVERAGE_HOURS)
        assert rule(2.0).confidence_scale == SHORT_COVERAGE_CONFIDENCE_FLOOR
        # A short lookback fully observed (young cluster) is not short coverage of the target.
        assert "short_coverage" not in rule(48.0, lookback=48.0).assumptions
        assert rule(0.0).hours == MIN_COVERAGE_HOURS
        # Without coverage, the baseline window is used as before.
        assert annualization(WorkloadBaseline(ref=ref, samples=(), window_hours=24.0)).hours == 24.0


class TestFirstObservedFromSummaries:
    def _snapshot(self, now: datetime) -> UsageSnapshot:
        snapshot = UsageSnapshot(source="rollup")
        snapshot.summaries[("apps", "Deployment", "web")] = {
            "": WorkloadUsageSummary("apps", "Deployment", "web", first_hour=now - timedelta(hours=10)),
            "app": WorkloadUsageSummary("apps", "Deployment", "web", "app", first_hour=now - timedelta(hours=5)),
            "sidecar": WorkloadUsageSummary(
                "apps", "Deployment", "web", "sidecar", first_hour=now - timedelta(hours=3)
            ),
        }
        snapshot.summaries[("apps", "Deployment", "api")] = {
            "": WorkloadUsageSummary("apps", "Deployment", "api", first_hour=now - timedelta(hours=20)),
        }
        return snapshot

    def test_lookup_order(self):
        now = _now()
        snapshot = self._snapshot(now)

        def first(kind: str, name: str, container=None):
            return first_observed_from_summaries(snapshot, WorkloadRef("", "apps", kind, name, container))

        assert first("Deployment", "web", "sidecar") == now - timedelta(hours=3)
        assert first("Deployment", "web") == now - timedelta(hours=10)
        assert first("Deployment", "web", "unknown") == now - timedelta(hours=10)
        assert first("Namespace", "apps") == now - timedelta(hours=20)
        assert first("Deployment", "missing") is None
