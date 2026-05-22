# tests/api/test_recommendations.py
"""Tests for the recommendations endpoint."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import get_config
from greenkube.models.metrics import RecommendationSavingsSummary


class TestRecommendationsEndpoint:
    """Tests for GET /api/v1/recommendations."""

    def test_recommendations_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/recommendations")
        assert response.status_code == 200

    def test_recommendations_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert data == []

    def test_recommendations_with_zombie_pod(self, client, mock_combined_metrics_repo):
        """Should detect a zombie pod (cost > threshold, energy < threshold)."""
        from datetime import datetime, timezone

        from greenkube.models.metrics import CombinedMetric

        zombie_metric = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.0,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[zombie_metric])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert len(data) >= 1
        assert any(r["type"] == "ZOMBIE_POD" for r in data)

    def test_recommendation_potential_savings_are_annualized(self, client, mock_combined_metrics_repo):
        """Potential savings returned by the API should be annual projections from the lookback window."""
        from greenkube.models.metrics import CombinedMetric

        zombie_metric = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.02,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=[zombie_metric])

        response = client.get("/api/v1/recommendations")

        assert response.status_code == 200
        zombie_rec = next(r for r in response.json() if r["type"] == "ZOMBIE_POD")
        annualization_factor = 365 / get_config().RECOMMENDATION_LOOKBACK_DAYS
        assert zombie_rec["potential_savings_cost"] == pytest.approx(0.05 * annualization_factor)
        assert zombie_rec["potential_savings_co2e_grams"] == pytest.approx(0.02 * annualization_factor)

    def test_recommendations_filter_by_namespace(self, client, mock_combined_metrics_repo):
        """Should filter recommendations by namespace."""
        from datetime import datetime, timezone

        from greenkube.models.metrics import CombinedMetric

        metrics = [
            CombinedMetric(
                pod_name="zombie-1",
                namespace="team-a",
                total_cost=0.05,
                co2e_grams=0.0,
                joules=100.0,
                cpu_request=100,
                memory_request=128 * 1024 * 1024,
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
                duration_seconds=300,
            ),
            CombinedMetric(
                pod_name="zombie-2",
                namespace="team-b",
                total_cost=0.05,
                co2e_grams=0.0,
                joules=100.0,
                cpu_request=100,
                memory_request=128 * 1024 * 1024,
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
                duration_seconds=300,
            ),
        ]
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/recommendations?namespace=team-a")
        data = response.json()
        assert all(r["namespace"] == "team-a" for r in data)

    def test_recommendations_contain_expected_fields(self, client, mock_combined_metrics_repo):
        """Each recommendation should contain expected fields."""
        from greenkube.models.metrics import CombinedMetric

        zombie = CombinedMetric(
            pod_name="zombie-pod",
            namespace="default",
            total_cost=0.05,
            co2e_grams=0.0,
            joules=100.0,
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[zombie])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        if data:
            rec = data[0]
            for field in ["pod_name", "namespace", "type", "description"]:
                assert field in rec, f"Missing field: {field}"

    def test_savings_summary_passes_last_time_window_to_repository(self, client, mock_reco_repo):
        """Savings should be filtered by the selected dashboard time window."""
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=42,
                total_cost_saved=1.5,
                applied_count=2,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d")

        assert response.status_code == 200
        kwargs = mock_reco_repo.get_savings_summary.await_args.kwargs
        assert kwargs["namespace"] == "prod"
        assert kwargs["start"] < kwargs["end"]
        assert kwargs["start"].tzinfo is not None
        assert kwargs["end"].tzinfo is not None

    def test_savings_summary_supports_ytd_window(self, client, mock_reco_repo):
        """YTD should resolve to January 1st of the current UTC year."""
        mock_reco_repo.get_savings_summary = AsyncMock(return_value=RecommendationSavingsSummary())

        response = client.get("/api/v1/recommendations/savings?last=ytd")

        assert response.status_code == 200
        start = mock_reco_repo.get_savings_summary.await_args.kwargs["start"]
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.tzinfo is not None

    def test_savings_summary_uses_ledger_for_selected_window(self, client, mock_reco_repo, mock_savings_repo):
        """Savings windows should count ongoing ledger rows, not only recommendations applied in the window."""
        mock_savings_repo.get_window_totals = AsyncMock(
            return_value={"RIGHTSIZING_CPU": {"co2e_saved_grams": 42.0, "cost_saved_dollars": 1.5}}
        )
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=0.0,
                total_cost_saved=0.0,
                applied_count=1,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod&last=7d")

        assert response.status_code == 200
        data = response.json()
        assert data["total_carbon_saved_co2e_grams"] == 42.0
        assert data["total_cost_saved"] == 1.5
        assert data["applied_count"] == 1
        mock_savings_repo.get_window_totals.assert_awaited_once()

    def test_savings_summary_without_window_uses_repository_summary(self, client, mock_reco_repo, mock_savings_repo):
        """Unbounded summaries should keep repository fallback semantics and namespace filtering."""
        mock_savings_repo.get_cumulative_totals = AsyncMock(
            return_value={"RIGHTSIZING_CPU": {"co2e_saved_grams": 999.0, "cost_saved_dollars": 99.0}}
        )
        mock_reco_repo.get_savings_summary = AsyncMock(
            return_value=RecommendationSavingsSummary(
                total_carbon_saved_co2e_grams=12.0,
                total_cost_saved=1.2,
                applied_count=2,
            )
        )

        response = client.get("/api/v1/recommendations/savings?namespace=prod")

        assert response.status_code == 200
        data = response.json()
        assert data["total_carbon_saved_co2e_grams"] == 12.0
        assert data["total_cost_saved"] == 1.2
        assert data["applied_count"] == 2
        mock_savings_repo.get_cumulative_totals.assert_not_awaited()
        mock_savings_repo.get_window_totals.assert_not_awaited()
