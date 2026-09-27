# tests/core/optimization/test_risks.py
"""Tests for risk, confidence and effort assessment."""

from greenkube.core.config import Config
from greenkube.core.optimization.risks import assess
from greenkube.models.evidence import RecommendationEvidence, ResourceSnapshot, UtilizationStats
from greenkube.models.metrics import (
    EffortLevel,
    Recommendation,
    RecommendationSource,
    RecommendationType,
    RiskLevel,
)


def _cpu_rec(source=RecommendationSource.GREENKUBE):
    return Recommendation(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="reduce cpu",
        source=source,
        recommended_cpu_request_millicores=300,
    )


def _evidence(cpu_stats=None, memory_stats=None, proposed_cpu=300, restart_count=0, sample_count=200):
    return RecommendationEvidence(
        sample_count=sample_count,
        coverage_ratio=0.98,
        proposed=ResourceSnapshot(cpu_request_millicores=proposed_cpu),
        cpu_usage=cpu_stats,
        memory_usage=memory_stats,
        restart_count=restart_count,
    )


class TestRightsizingRisk:
    def test_low_risk_with_headroom(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=150, max=180))
        result = assess(_cpu_rec(), evidence, Config())
        assert result.risk_level == RiskLevel.LOW
        assert "sufficient_headroom" in result.risk_factors

    def test_medium_risk_when_p99_close(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=290, max=295))
        result = assess(_cpu_rec(), evidence, Config())
        assert result.risk_level == RiskLevel.MEDIUM
        assert "p99_close_to_proposal" in result.risk_factors

    def test_high_risk_when_max_exceeds_proposal(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=300, max=350))
        result = assess(_cpu_rec(), evidence, Config())
        assert result.risk_level == RiskLevel.HIGH
        assert "observed_max_exceeds_proposal" in result.risk_factors

    def test_missing_evidence_is_medium(self):
        result = assess(_cpu_rec(), None, Config())
        assert result.risk_level == RiskLevel.MEDIUM
        assert "insufficient_evidence" in result.risk_factors


class TestOtherTypes:
    def test_memory_restart_history_is_high(self):
        rec = _cpu_rec()
        rec = rec.model_copy(update={"type": RecommendationType.RIGHTSIZING_MEMORY})
        evidence = _evidence(restart_count=2)
        result = assess(rec, evidence, Config())
        assert result.risk_level == RiskLevel.HIGH
        assert "restart_history" in result.risk_factors

    def test_node_recommendation_is_high_effort(self):
        rec = Recommendation(
            type=RecommendationType.UNDERUTILIZED_NODE,
            scope="node",
            target_node="node-1",
            description="drain",
        )
        result = assess(rec, None, Config())
        assert result.risk_level == RiskLevel.HIGH
        assert result.effort == EffortLevel.HIGH
        assert result.requires_restart is True

    def test_cleanup_recommendations_are_low_risk(self):
        rec = Recommendation(
            pod_name="pv-1",
            type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
            scope="cluster",
            description="delete pv",
        )
        result = assess(rec, None, Config())
        assert result.risk_level == RiskLevel.LOW
        assert result.reversible is False


class TestConfidence:
    def test_high_confidence_with_samples_and_coverage(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=150, max=180))
        result = assess(_cpu_rec(), evidence, Config())
        assert result.confidence >= 0.8
        assert "samples" in result.confidence_factors
        assert "coverage" in result.confidence_factors

    def test_vpa_source_adds_authority(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=150, max=180))
        native = assess(_cpu_rec(RecommendationSource.GREENKUBE), evidence, Config())
        vpa = assess(_cpu_rec(RecommendationSource.VPA), evidence, Config())
        assert vpa.confidence > native.confidence

    def test_low_sample_count_reduces_confidence(self):
        evidence = _evidence(cpu_stats=UtilizationStats(avg=100, p99=150, max=180), sample_count=2)
        result = assess(_cpu_rec(), evidence, Config())
        assert result.confidence < 0.7
