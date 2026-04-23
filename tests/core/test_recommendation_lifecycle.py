# tests/core/test_recommendation_lifecycle.py
"""
Tests for the recommendation lifecycle:
- Minimum threshold clamping by the Recommender
- RecommendationRecord status transitions
- Savings summary computation
- API endpoint behaviour (mocked repository)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.recommender import Recommender
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    CombinedMetric,
    IgnoreRecommendationRequest,
    Recommendation,
    RecommendationRecord,
    RecommendationSavingsSummary,
    RecommendationStatus,
    RecommendationType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metric(
    pod="pod-a",
    ns="default",
    cpu_req=500,
    cpu_usage=50,
    mem_req=512 * 1024 * 1024,
    mem_usage=50 * 1024 * 1024,
    cost=1.0,
    joules=50000,
):
    return CombinedMetric(
        pod_name=pod,
        namespace=ns,
        cpu_request=cpu_req,
        memory_request=mem_req,
        cpu_usage_millicores=cpu_usage,
        memory_usage_bytes=mem_usage,
        total_cost=cost,
        joules=joules,
        co2e_grams=50.0,
        timestamp=datetime.now(timezone.utc),
        duration_seconds=3600,
    )


# ---------------------------------------------------------------------------
# Minimum threshold clamping
# ---------------------------------------------------------------------------


class TestMinimumThresholdClamping:
    """Tests that impractically small recommended values are clamped, not rejected."""

    def test_cpu_below_minimum_is_clamped(self):
        """A recommendation with suggested CPU < 10m is clamped to 10m."""
        recommender = Recommender()
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU",
            recommended_cpu_request_millicores=3,  # Below 10m default
        )
        result = recommender._apply_minimum_thresholds(rec)
        assert result.recommended_cpu_request_millicores == 10
        assert "Floored to minimum" in result.description

    def test_cpu_at_or_above_minimum_is_unchanged(self):
        """A recommendation with suggested CPU >= 10m is left unchanged."""
        recommender = Recommender()
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU",
            recommended_cpu_request_millicores=50,
        )
        result = recommender._apply_minimum_thresholds(rec)
        assert result.recommended_cpu_request_millicores == 50
        assert "Floored" not in result.description

    def test_memory_below_minimum_is_clamped(self):
        """A recommendation with suggested memory < 16MiB is clamped."""
        recommender = Recommender()
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_MEMORY,
            description="Reduce memory",
            recommended_memory_request_bytes=3 * 1024 * 1024,  # 3MiB < 16MiB
        )
        result = recommender._apply_minimum_thresholds(rec)
        assert result.recommended_memory_request_bytes == 16 * 1024 * 1024
        assert "Floored to minimum" in result.description

    def test_both_cpu_and_memory_clamped_together(self):
        """Both CPU and memory can be clamped in a single recommendation."""
        recommender = Recommender()
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce resources",
            recommended_cpu_request_millicores=2,
            recommended_memory_request_bytes=4 * 1024 * 1024,
        )
        result = recommender._apply_minimum_thresholds(rec)
        assert result.recommended_cpu_request_millicores == 10
        assert result.recommended_memory_request_bytes == 16 * 1024 * 1024

    def test_generate_recommendations_clamps_values(self):
        """generate_recommendations clamps impractically small recommended values."""
        # Very low cpu_usage forces a tiny recommended value before clamping
        metrics = [_make_metric(cpu_req=1000, cpu_usage=2) for _ in range(10)]
        recommender = Recommender()
        recs = recommender.generate_recommendations(metrics)
        rightsizing = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        for r in rightsizing:
            if r.recommended_cpu_request_millicores is not None:
                assert r.recommended_cpu_request_millicores >= recommender.min_cpu_millicores


# ---------------------------------------------------------------------------
# RecommendationRecord model
# ---------------------------------------------------------------------------


class TestRecommendationRecord:
    """Tests for the RecommendationRecord model and status lifecycle."""

    def test_from_recommendation_creates_active_record(self):
        """from_recommendation always creates an active record."""
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.ZOMBIE_POD,
            description="Zombie pod",
        )
        record = RecommendationRecord.from_recommendation(rec)
        assert record.status == RecommendationStatus.ACTIVE
        assert record.applied_at is None
        assert record.ignored_at is None

    def test_from_recommendation_preserves_clamped_values(self):
        """from_recommendation copies recommended values as-is (already clamped by engine)."""
        rec = Recommendation(
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Reduce CPU (Floored to minimum: 10m CPU.)",
            recommended_cpu_request_millicores=10,  # Already clamped
        )
        record = RecommendationRecord.from_recommendation(rec)
        assert record.recommended_cpu_request_millicores == 10
        assert record.status == RecommendationStatus.ACTIVE


# ---------------------------------------------------------------------------
# Savings summary
# ---------------------------------------------------------------------------


class TestSavingsSummary:
    """Tests for the savings summary model."""

    def test_empty_savings_summary(self):
        summary = RecommendationSavingsSummary()
        assert summary.total_carbon_saved_co2e_grams == 0.0
        assert summary.total_cost_saved == 0.0
        assert summary.applied_count == 0
        assert summary.namespace_breakdown == []

    def test_savings_summary_with_data(self):
        summary = RecommendationSavingsSummary(
            total_carbon_saved_co2e_grams=500.0,
            total_cost_saved=12.5,
            applied_count=3,
            namespace_breakdown=[
                {"namespace": "prod", "carbon_saved_co2e_grams": 300.0, "cost_saved": 8.0, "count": 2},
                {"namespace": "staging", "carbon_saved_co2e_grams": 200.0, "cost_saved": 4.5, "count": 1},
            ],
        )
        assert summary.total_carbon_saved_co2e_grams == 500.0
        assert summary.applied_count == 3
        assert len(summary.namespace_breakdown) == 2


# ---------------------------------------------------------------------------
# Repository mock: apply / ignore / unignore
# ---------------------------------------------------------------------------


class TestRecommendationRepositoryLifecycle:
    """Tests for lifecycle operations using a mocked repository."""

    @pytest.fixture
    def base_record(self):
        return RecommendationRecord(
            id=1,
            pod_name="pod-a",
            namespace="default",
            type=RecommendationType.ZOMBIE_POD,
            description="Zombie pod",
            status=RecommendationStatus.ACTIVE,
            potential_savings_co2e_grams=120.0,
            potential_savings_cost=5.0,
            created_at=datetime.now(timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_apply_recommendation_uses_potential_savings_as_default(self, base_record):
        """When savings are not provided, potential savings are used as the estimate."""
        repo = AsyncMock()
        applied_record = base_record.model_copy(
            update={
                "status": RecommendationStatus.APPLIED,
                "applied_at": datetime.now(timezone.utc),
                "carbon_saved_co2e_grams": base_record.potential_savings_co2e_grams,
                "cost_saved": base_record.potential_savings_cost,
            }
        )
        repo.apply_recommendation.return_value = applied_record

        request = ApplyRecommendationRequest(
            actual_cpu_request_millicores=50,
            actual_memory_request_bytes=None,
        )
        result = await repo.apply_recommendation(1, request)

        assert result.status == RecommendationStatus.APPLIED
        assert result.carbon_saved_co2e_grams == 120.0
        assert result.cost_saved == 5.0
        repo.apply_recommendation.assert_called_once_with(1, request)

    @pytest.mark.asyncio
    async def test_ignore_recommendation(self, base_record):
        """Ignoring a recommendation sets status=ignored and records the reason."""
        repo = AsyncMock()
        ignored_record = base_record.model_copy(
            update={
                "status": RecommendationStatus.IGNORED,
                "ignored_at": datetime.now(timezone.utc),
                "ignored_reason": "Pod uses RWO PVC, HPA not supported.",
            }
        )
        repo.ignore_recommendation.return_value = ignored_record

        request = IgnoreRecommendationRequest(reason="Pod uses RWO PVC, HPA not supported.")
        result = await repo.ignore_recommendation(1, request)

        assert result.status == RecommendationStatus.IGNORED
        assert result.ignored_reason == "Pod uses RWO PVC, HPA not supported."

    @pytest.mark.asyncio
    async def test_unignore_recommendation(self, base_record):
        """Un-ignoring a recommendation restores active status."""
        repo = AsyncMock()
        restored_record = base_record.model_copy(
            update={
                "status": RecommendationStatus.ACTIVE,
                "ignored_at": None,
                "ignored_reason": None,
            }
        )
        repo.unignore_recommendation.return_value = restored_record

        result = await repo.unignore_recommendation(1)
        assert result.status == RecommendationStatus.ACTIVE
        assert result.ignored_reason is None

    @pytest.mark.asyncio
    async def test_get_active_returns_only_active_status(self):
        """get_active_recommendations returns only status=active records."""
        repo = AsyncMock()
        repo.get_active_recommendations.return_value = [
            RecommendationRecord(
                id=2,
                pod_name="pod-b",
                namespace="default",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="Reduce CPU",
                status=RecommendationStatus.ACTIVE,
                created_at=datetime.now(timezone.utc),
            )
        ]
        results = await repo.get_active_recommendations()
        assert all(r.status == RecommendationStatus.ACTIVE for r in results)

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self):
        """Applying or ignoring a non-existent recommendation raises ValueError."""
        repo = AsyncMock()
        repo.apply_recommendation.side_effect = ValueError("Recommendation 999 not found.")

        with pytest.raises(ValueError, match="999"):
            await repo.apply_recommendation(999, ApplyRecommendationRequest())
