# tests/core/optimization/test_enrich.py
"""Tests for evidence/risk/ranking enrichment of generated recommendations."""

from datetime import datetime, timedelta, timezone

from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _context(count=10):
    metrics = [
        CombinedMetric(
            pod_name="api-7d9f8b6c5-x2k4p",
            namespace="prod",
            owner_kind="Deployment",
            owner_name="api",
            cpu_request=1000,
            memory_request=2 * 1024**3,
            cpu_usage_millicores=200,
            cpu_usage_max_millicores=300,
            memory_usage_bytes=512 * 1024**2,
            memory_usage_max_bytes=600 * 1024**2,
            total_cost=0.01,
            co2e_grams=1.0,
            sample_count=12,
            timestamp=NOW + timedelta(hours=i),
        )
        for i in range(count)
    ]
    return OptimizationContext(
        config=Config(),
        metrics=metrics,
        analysis_window_seconds=count * 3600,
        window_start=NOW,
        window_end=NOW + timedelta(hours=count),
    )


def _rec():
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


class TestEnrichRecommendations:
    def test_sets_evidence_risk_and_ranking(self):
        config = Config()
        enriched = enrich_recommendations([_rec()], _context(), config)

        assert len(enriched) == 1
        rec = enriched[0]
        assert rec.evidence is not None
        assert rec.risk_level is not None
        assert rec.confidence is not None
        assert rec.effort is not None
        assert rec.patch is not None
        assert rec.ranking_score is not None
        assert rec.ranking_factors
        assert rec.expires_at is not None
        assert rec.reversible is True
        assert rec.requires_restart is True

    def test_evidence_carries_risk_and_expiry(self):
        config = Config()
        rec = enrich_recommendations([_rec()], _context(), config)[0]
        assert rec.evidence is not None
        assert rec.risk_level is not None
        assert rec.expires_at is not None
        assert rec.evidence.risk_level == rec.risk_level.value
        assert rec.evidence.expires_at == rec.expires_at
        assert rec.evidence.proposed_patch == rec.patch

    def test_expiry_uses_configured_ttl(self):
        config = Config()
        config.RECOMMENDATION_TTL_DAYS = 3
        rec = enrich_recommendations([_rec()], _context(), config)[0]
        assert rec.expires_at is not None
        delta = rec.expires_at - datetime.now(timezone.utc)
        assert 2.9 < delta.total_seconds() / 86400 <= 3

    def test_ranking_profile_is_configurable(self):
        config = Config()
        config.RECOMMENDATION_RANKING_PROFILE = "low_risk"
        rec = enrich_recommendations([_rec()], _context(), config)[0]
        assert rec.ranking_factors
        assert rec.ranking_score is not None
