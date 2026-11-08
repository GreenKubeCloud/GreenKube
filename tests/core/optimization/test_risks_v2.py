# tests/core/optimization/test_risks_v2.py
"""Risk model v2: workload facts the pooled usage statistics cannot show (roadmap I4)."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.enrich import build_risk_profile, enrich_recommendations
from greenkube.core.optimization.risks import WorkloadRiskProfile, assess, profile_from_binding_evidence
from greenkube.models.evidence import RecommendationEvidence, ResourceSnapshot, UtilizationStats
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType, RiskLevel


def _cpu_rec(proposed=300, current=1000):
    return Recommendation(
        pod_name="api",
        container_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="reduce cpu",
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
    )


def _memory_rec(proposed=256 * 1024**2, current=1024**3):
    return Recommendation(
        pod_name="api",
        container_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="reduce memory",
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=proposed,
    )


def _cpu_evidence():
    # Pooled distribution with ample headroom: low risk without workload facts.
    return RecommendationEvidence(
        sample_count=200,
        coverage_ratio=0.98,
        proposed=ResourceSnapshot(cpu_request_millicores=300),
        cpu_usage=UtilizationStats(avg=100, p95=140, p99=150, max=180),
    )


def _memory_evidence():
    return RecommendationEvidence(
        sample_count=200,
        coverage_ratio=0.98,
        proposed=ResourceSnapshot(memory_request_bytes=256 * 1024**2),
        memory_usage=UtilizationStats(avg=100 * 1024**2, p95=120 * 1024**2, max=150 * 1024**2),
    )


def _risk(rec, evidence, **profile):
    return assess(rec, evidence, Config(), WorkloadRiskProfile(**profile))


class TestRiskModelV2:
    def test_unknown_facts_never_change_the_risk(self):
        result = assess(_cpu_rec(), _cpu_evidence(), Config(), WorkloadRiskProfile())
        assert result.risk_level == RiskLevel.LOW
        assert result.risk_factors == ["sufficient_headroom"]

    def test_hot_replica_hidden_by_the_pool_is_high_risk(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=4, replica_cpu_p95_max=320.0)
        assert result.risk_level == RiskLevel.HIGH
        assert "replica_p95_exceeds_proposal" in result.risk_factors

    def test_replica_close_to_the_proposal_is_medium(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=4, replica_cpu_p95_max=280.0)
        assert result.risk_level == RiskLevel.MEDIUM
        assert "replica_p95_close_to_proposal" in result.risk_factors

    def test_singleton_raises_one_level(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=1)
        assert result.risk_level == RiskLevel.MEDIUM
        assert "singleton_workload" in result.risk_factors

    def test_oom_history_makes_memory_decreases_high(self):
        result = _risk(_memory_rec(), _memory_evidence(), replicas=3, oom_events=2)
        assert result.risk_level == RiskLevel.HIGH
        assert "oom_history" in result.risk_factors

    def test_oom_history_on_a_reliability_increase_is_the_avoided_incident(self):
        # Two-way rightsizing (E9): an increase fixes the OOM kills, it is not made riskier by them.
        evidence = RecommendationEvidence(
            sample_count=200,
            coverage_ratio=0.98,
            restart_count=3,
            proposed=ResourceSnapshot(memory_request_bytes=2 * 1024**3),
            memory_usage=UtilizationStats(avg=900 * 1024**2, p95=1000 * 1024**2, max=1100 * 1024**2),
        )
        rec = _memory_rec(proposed=2 * 1024**3, current=1024**3)
        result = _risk(rec, evidence, replicas=3, oom_events=2)
        assert result.risk_level == RiskLevel.LOW
        assert {"restart_history", "oom_history"} <= set(result.risk_factors)

    def test_cpu_throttling_on_a_reliability_increase_keeps_the_risk(self):
        rec = _cpu_rec(proposed=1500, current=1000)
        evidence = RecommendationEvidence(
            sample_count=200,
            coverage_ratio=0.98,
            proposed=ResourceSnapshot(cpu_request_millicores=1500),
            cpu_usage=UtilizationStats(avg=800, p95=950, p99=980, max=1000),
        )
        result = _risk(rec, evidence, replicas=3, cpu_throttling_ratio=0.4)
        assert result.risk_level == RiskLevel.LOW
        assert "cpu_throttling_history" in result.risk_factors

    @pytest.mark.parametrize("ratio,level", [(0.1, RiskLevel.MEDIUM), (0.4, RiskLevel.HIGH), (0.01, RiskLevel.LOW)])
    def test_cpu_throttling_history(self, ratio, level):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, cpu_throttling_ratio=ratio)
        assert result.risk_level == level

    def test_hpa_scaling_on_the_changed_resource(self):
        on_cpu = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, hpa_resources=frozenset({"cpu"}))
        on_memory = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, hpa_resources=frozenset({"memory"}))
        unknown = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, hpa_resources=frozenset({"*"}))
        assert on_cpu.risk_level == RiskLevel.MEDIUM and "hpa_scales_on_resource" in on_cpu.risk_factors
        assert on_memory.risk_level == RiskLevel.LOW
        assert unknown.risk_level == RiskLevel.LOW and "hpa_present" in unknown.risk_factors

    def test_vpa_writer_conflicts(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, vpa_update_mode="Auto")
        assert result.risk_level == RiskLevel.HIGH
        assert "vpa_conflicting_writer" in result.risk_factors
        assert _risk(_cpu_rec(), _cpu_evidence(), replicas=3, vpa_update_mode="Off").risk_level == RiskLevel.LOW

    def test_pdb_conflict_is_at_least_medium(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, pdb_conflict=True)
        assert result.risk_level == RiskLevel.MEDIUM
        assert "pdb_blocks_disruption" in result.risk_factors

    def test_jvm_flag(self):
        memory = _risk(_memory_rec(), _memory_evidence(), replicas=3, jvm=True)
        cpu = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, jvm=True)
        assert memory.risk_level == RiskLevel.HIGH and "jvm_heap_not_observable" in memory.risk_factors
        assert cpu.risk_level == RiskLevel.MEDIUM and "jvm_startup_cpu" in cpu.risk_factors

    def test_in_place_resize_constraints(self):
        memory = _risk(_memory_rec(), _memory_evidence(), replicas=3)
        static = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, static_cpu_manager=True)
        assert "memory_decrease_best_effort" in memory.risk_factors
        assert memory.risk_level == RiskLevel.LOW
        assert static.risk_level == RiskLevel.HIGH and "static_cpu_manager_incompatible" in static.risk_factors

    @pytest.mark.parametrize(
        "criticality,level",
        [("critical", RiskLevel.MEDIUM), ("high", RiskLevel.MEDIUM), ("normal", RiskLevel.LOW), ("low", RiskLevel.LOW)],
    )
    def test_criticality_label(self, criticality, level):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=3, criticality=criticality)
        assert result.risk_level == level

    def test_critical_singleton_with_a_hot_replica_stays_high(self):
        result = _risk(_cpu_rec(), _cpu_evidence(), replicas=1, replica_cpu_p95_max=500.0, criticality="critical")
        assert result.risk_level == RiskLevel.HIGH

    def test_cleanup_recommendations_ignore_restart_facts(self):
        zombie = Recommendation(
            pod_name="old", namespace="prod", type=RecommendationType.ZOMBIE_POD, description="delete"
        )
        result = assess(zombie, None, Config(), WorkloadRiskProfile(replicas=1, pdb_conflict=True))
        assert result.risk_level == RiskLevel.LOW


class TestBindingEvidenceProfile:
    def test_reads_labels_annotations_and_interactions(self):
        profile = profile_from_binding_evidence(
            {
                "workload_labels": {"greenkube.io/criticality": "Critical", "app": "api"},
                "workload": {"labels": {}, "annotations": {"greenkube.io/jvm": "true"}},
                "hpa_pdb_interactions_checked": True,
                "hpa_targets": [{"name": "api", "resource_metrics": [{"name": "cpu"}]}],
                "pdb_conflict": True,
                "vpa_interactions_checked": True,
                "vpa_targets": [{"name": "api", "update_mode": "Recreate"}],
            }
        )
        assert profile.criticality == "critical"
        assert profile.jvm is True
        assert profile.hpa_resources == frozenset({"cpu"})
        assert profile.pdb_conflict is True
        assert profile.vpa_update_mode == "Recreate"

    def test_runtime_label_flags_jvm(self):
        assert profile_from_binding_evidence({"workload_labels": {"greenkube.io/runtime": "jvm"}}).jvm is True

    def test_unchecked_interactions_stay_unknown(self):
        profile = profile_from_binding_evidence({"workload_labels": {"greenkube.io/criticality": "bogus"}})
        assert profile == WorkloadRiskProfile()
        assert profile_from_binding_evidence(None) == WorkloadRiskProfile()


def _metric(pod, ts, cpu):
    return CombinedMetric(
        pod_name=pod,
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        container_name="api",
        cpu_request=1000,
        cpu_usage_millicores=cpu,
        memory_request=1024**3,
        memory_usage_bytes=100 * 1024**2,
        timestamp=ts,
        duration_seconds=300,
    )


class TestScanProfile:
    def test_counts_current_replicas_and_the_hottest_replica(self):
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        metrics = []
        for step in range(12):
            ts = start + timedelta(minutes=5 * step)
            metrics += [_metric("api-6d4b8c7f9-aaaaa", ts, 50), _metric("api-6d4b8c7f9-bbbbb", ts, 400)]
        context = OptimizationContext(
            config=Config(), container_metrics=metrics, hpa_targets={("prod", "Deployment", "api")}
        )
        profile = build_risk_profile(_cpu_rec(), context)
        assert profile.replicas == 2
        assert profile.replica_cpu_p95_max == pytest.approx(400.0)
        assert profile.hpa_resources == frozenset({"*"})

    def test_counts_replicas_scraped_with_jittered_timestamps(self):
        """Replicas sampled a few seconds apart are not mistaken for a singleton."""
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        metrics = []
        for step in range(12):
            ts = start + timedelta(minutes=5 * step)
            metrics += [
                _metric("api-6d4b8c7f9-aaaaa", ts, 50),
                _metric("api-6d4b8c7f9-bbbbb", ts + timedelta(seconds=3), 60),
                _metric("api-6d4b8c7f9-ccccc", ts + timedelta(seconds=7), 70),
            ]
        profile = build_risk_profile(_cpu_rec(), OptimizationContext(config=Config(), container_metrics=metrics))
        assert profile.replicas == 3

    def test_a_pod_replaced_by_a_rollout_is_not_counted(self):
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        metrics = [_metric("api-old-aaaaa", start + timedelta(minutes=5 * step), 50) for step in range(11)]
        metrics.append(_metric("api-new-bbbbb", start + timedelta(minutes=55), 50))
        profile = build_risk_profile(_cpu_rec(), OptimizationContext(config=Config(), container_metrics=metrics))
        assert profile.replicas == 1

    def test_enrich_flags_a_hot_replica(self):
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        metrics = []
        for step in range(12):
            ts = start + timedelta(minutes=5 * step)
            metrics += [_metric(f"api-6d4b8c7f9-{c * 5}", ts, cpu) for c, cpu in (("a", 20), ("b", 20), ("c", 350))]
        context = OptimizationContext(
            config=Config(),
            container_metrics=metrics,
            analysis_window_seconds=7200,
            window_start=start,
            window_end=start + timedelta(hours=2),
        )
        (rec,) = enrich_recommendations([_cpu_rec(proposed=360)], context, Config())
        assert "replica_p95_close_to_proposal" in rec.risk_factors
        assert rec.risk_level == RiskLevel.MEDIUM
        assert rec.value is not None and rec.ranking_score == rec.value["ev"]
