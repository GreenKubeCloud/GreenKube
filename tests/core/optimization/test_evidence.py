# tests/core/optimization/test_evidence.py
"""Tests for the review evidence builder."""

from datetime import datetime, timedelta, timezone

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.evidence import build_evidence, build_patch
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _series(count=10, cpu_request=1000, memory_request=2 * 1024**3, restart_count=0):
    metrics = []
    for i in range(count):
        metrics.append(
            CombinedMetric(
                pod_name="api-7d9f8b6c5-x2k4p",
                namespace="prod",
                owner_kind="Deployment",
                owner_name="api",
                cpu_request=cpu_request,
                memory_request=memory_request,
                cpu_usage_millicores=200 + i * 10,
                cpu_usage_max_millicores=300 + i * 10,
                memory_usage_bytes=512 * 1024**2 + i * 1024**2,
                memory_usage_max_bytes=600 * 1024**2 + i * 1024**2,
                restart_count=restart_count,
                total_cost=0.01,
                co2e_grams=1.0,
                sample_count=12,
                timestamp=NOW + timedelta(hours=i),
            )
        )
    return metrics


def _context(metrics):
    return OptimizationContext(
        config=Config(),
        metrics=metrics,
        analysis_window_seconds=len(metrics) * 3600,
        window_start=NOW,
        window_end=NOW + timedelta(hours=len(metrics)),
    )


def _cpu_rec():
    return Recommendation(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="reduce cpu",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=10.0,
        potential_savings_co2e_grams=100.0,
    )


class TestBuildEvidence:
    def test_populates_window_snapshots_and_distribution(self):
        context = _context(_series())
        evidence = build_evidence(_cpu_rec(), context)

        assert evidence is not None
        assert evidence.sample_count == 120
        assert evidence.observation_window_seconds == 10 * 3600
        assert evidence.current.cpu_request_millicores == 1000
        assert evidence.proposed.cpu_request_millicores == 300
        assert evidence.cpu_usage is not None
        assert evidence.cpu_usage.max == 390
        assert evidence.cpu_usage.p50 > 0
        assert evidence.changes[0].change_ratio == 0.7
        assert evidence.savings_method == "request_reduction_ratio"
        assert evidence.expected_savings_cost_annual == 10.0

    def test_includes_rollback_conditions_for_rightsizing(self):
        evidence = build_evidence(_cpu_rec(), _context(_series()))
        assert evidence is not None
        metrics = {c.metric for c in evidence.rollback_conditions}
        assert "cpu_p95_usage" in metrics
        assert "restart_rate" in metrics

    def test_builds_patch_plan(self):
        evidence = build_evidence(_cpu_rec(), _context(_series()))
        assert evidence is not None
        assert evidence.proposed_patch is not None
        assert evidence.proposed_patch["kind"] == "Deployment"
        assert evidence.proposed_patch["operations"][0]["resource"] == "cpu"
        assert evidence.proposed_patch["operations"][0]["value"] == "300m"

    def test_returns_none_without_metrics(self):
        assert build_evidence(_cpu_rec(), _context([])) is None


class TestBuildPatch:
    def test_memory_value_is_humanized(self):
        rec = Recommendation(
            pod_name="api",
            namespace="prod",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            scope="workload",
            owner_kind="Deployment",
            owner_name="api",
            description="reduce mem",
            recommended_memory_request_bytes=512 * 1024**2,
        )
        patch = build_patch(rec)
        assert patch is not None
        assert patch["operations"][0]["value"] == "512Mi"

    def test_no_patch_for_non_rightsizing(self):
        rec = Recommendation(
            pod_name="zombie",
            namespace="prod",
            type=RecommendationType.ZOMBIE_POD,
            scope="pod",
            description="delete",
        )
        assert build_patch(rec) is None

    def test_no_patch_without_owner(self):
        rec = _cpu_rec()
        rec = rec.model_copy(update={"owner_kind": None, "owner_name": None})
        assert build_patch(rec) is None
