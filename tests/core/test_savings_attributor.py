# tests/core/test_savings_attributor.py
"""Tests for SavingsAttributor — the service that prorates annual recommendation
savings into per-period time-series records."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.savings_attributor import _SECONDS_PER_YEAR, SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


def _make_applied_rec(
    rec_id: int = 1,
    co2e_annual: float = 150.5,
    cost_annual: float = 12.3,
    rec_type: RecommendationType = RecommendationType.OVERPROVISIONED_NODE,
    namespace: str = "default",
) -> RecommendationRecord:
    return RecommendationRecord(
        id=rec_id,
        pod_name="test-pod",
        namespace=namespace,
        type=rec_type,
        description="Test recommendation",
        reason="overprovisioned",
        priority="high",
        scope="node",
        status=RecommendationStatus.APPLIED,
        carbon_saved_co2e_grams=co2e_annual,
        cost_saved=cost_annual,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class TestSavingsAttributorProratedCalculation:
    """Unit tests for per-period savings proration."""

    def test_prorates_annual_co2_to_5_minute_period(self):
        rec = _make_applied_rec(co2e_annual=52560.0, cost_annual=8760.0)
        attributor = SavingsAttributor(
            savings_repo=AsyncMock(),
            cluster_name="test-cluster",
        )
        records = attributor._compute_period_records([rec], period_seconds=300)

        assert len(records) == 1
        expected_co2 = 52560.0 * 300 / _SECONDS_PER_YEAR
        expected_cost = 8760.0 * 300 / _SECONDS_PER_YEAR
        assert records[0].co2e_saved_grams == pytest.approx(expected_co2, rel=1e-4)
        assert records[0].cost_saved_dollars == pytest.approx(expected_cost, rel=1e-4)

    def test_prorates_real_recommendation(self):
        """Verify the real-world OVERPROVISIONED_NODE recommendation (150.5 g/yr, $12.3/yr)."""
        rec = _make_applied_rec(co2e_annual=150.5, cost_annual=12.3)
        attributor = SavingsAttributor(savings_repo=AsyncMock(), cluster_name="minikube")
        records = attributor._compute_period_records([rec], period_seconds=300)

        assert len(records) == 1
        expected = 150.5 * 300 / _SECONDS_PER_YEAR
        assert records[0].co2e_saved_grams == pytest.approx(expected, rel=1e-4)
        assert records[0].cost_saved_dollars == pytest.approx(12.3 * 300 / _SECONDS_PER_YEAR, rel=1e-4)
        assert records[0].recommendation_id == 1
        assert records[0].cluster_name == "minikube"
        assert records[0].namespace == "default"
        assert records[0].recommendation_type == "OVERPROVISIONED_NODE"

    def test_skips_recommendation_without_co2_data(self):
        """Recommendations without carbon_saved_co2e_grams should be skipped."""
        rec = _make_applied_rec(co2e_annual=None, cost_annual=None)
        attributor = SavingsAttributor(savings_repo=AsyncMock(), cluster_name="test")
        records = attributor._compute_period_records([rec], period_seconds=300)

        assert records == []

    def test_skips_recommendation_with_zero_co2(self):
        rec = _make_applied_rec(co2e_annual=0.0, cost_annual=0.0)
        attributor = SavingsAttributor(savings_repo=AsyncMock(), cluster_name="test")
        records = attributor._compute_period_records([rec], period_seconds=300)

        assert records == []

    def test_multiple_applied_recommendations(self):
        recs = [
            _make_applied_rec(rec_id=1, co2e_annual=52560.0, namespace="ns-a"),
            _make_applied_rec(rec_id=2, co2e_annual=52560.0, namespace="ns-b"),
        ]
        attributor = SavingsAttributor(savings_repo=AsyncMock(), cluster_name="test")
        records = attributor._compute_period_records(recs, period_seconds=300)

        assert len(records) == 2
        assert {r.namespace for r in records} == {"ns-a", "ns-b"}

    def test_period_seconds_scaling(self):
        """Larger periods produce proportionally larger savings."""
        rec = _make_applied_rec(co2e_annual=52560.0)
        attributor = SavingsAttributor(savings_repo=AsyncMock(), cluster_name="test")

        r_5min = attributor._compute_period_records([rec], period_seconds=300)[0]
        r_1h = attributor._compute_period_records([rec], period_seconds=3600)[0]

        assert r_1h.co2e_saved_grams == pytest.approx(r_5min.co2e_saved_grams * 12, rel=1e-4)


class TestSavingsAttributorIntegration:
    """Integration tests for the full attribute_period flow."""

    @pytest.mark.asyncio
    async def test_attribute_period_writes_to_repo(self):
        mock_repo = AsyncMock()
        mock_repo.save_records = AsyncMock(return_value=1)
        rec = _make_applied_rec(co2e_annual=150.5, cost_annual=12.3)

        attributor = SavingsAttributor(savings_repo=mock_repo, cluster_name="minikube")
        count = await attributor.attribute_period([rec], period_seconds=300)

        assert count == 1
        mock_repo.save_records.assert_awaited_once()
        saved_records = mock_repo.save_records.call_args[0][0]
        assert len(saved_records) == 1
        assert saved_records[0].co2e_saved_grams == pytest.approx(150.5 * 300 / _SECONDS_PER_YEAR, rel=1e-4)

    @pytest.mark.asyncio
    async def test_attribute_period_returns_zero_for_no_applicable_recs(self):
        mock_repo = AsyncMock()
        mock_repo.save_records = AsyncMock(return_value=0)

        attributor = SavingsAttributor(savings_repo=mock_repo, cluster_name="minikube")
        count = await attributor.attribute_period([], period_seconds=300)

        assert count == 0
        mock_repo.save_records.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attribute_period_does_not_crash_on_repo_error(self):
        """Errors in the repo must not propagate (collection continues)."""
        mock_repo = AsyncMock()
        mock_repo.save_records = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        rec = _make_applied_rec(co2e_annual=150.5)

        attributor = SavingsAttributor(savings_repo=mock_repo, cluster_name="minikube")
        # Should not raise
        count = await attributor.attribute_period([rec], period_seconds=300)
        assert count == 0

    @pytest.mark.asyncio
    async def test_get_cumulative_totals_returns_aggregated_values(self):
        mock_repo = AsyncMock()
        mock_repo.get_cumulative_totals = AsyncMock(
            return_value={"OVERPROVISIONED_NODE": {"co2e_saved_grams": 42.0, "cost_saved_dollars": 3.5}}
        )
        attributor = SavingsAttributor(savings_repo=mock_repo, cluster_name="minikube")
        totals = await attributor.get_cumulative_totals()

        assert totals["OVERPROVISIONED_NODE"]["co2e_saved_grams"] == pytest.approx(42.0)
        mock_repo.get_cumulative_totals.assert_awaited_once_with(cluster_name="minikube")
