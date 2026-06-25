# tests/api/test_metrics.py
"""Tests for the metrics endpoints."""

from unittest.mock import AsyncMock

import pytest


class TestMetricsListEndpoint:
    """Tests for GET /api/v1/metrics."""

    def test_metrics_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200

    def test_metrics_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/metrics")
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_metrics_returns_data(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should return metrics from the repository via DB-level pagination."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics")
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["pod_name"] == "nginx-abc123"
        assert data["items"][1]["pod_name"] == "api-server-xyz"

    def test_metrics_filter_by_namespace(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should filter metrics by namespace query param."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics?namespace=production")
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["namespace"] == "production"

    def test_metrics_filter_by_last(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should accept --last style time filter."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics?last=7d")
        assert response.status_code == 200

    def test_metrics_supports_ytd_last_returns_400(self, client):
        """YTD spans > 30 days and must be rejected to avoid OOM."""
        response = client.get("/api/v1/metrics?last=ytd")
        assert response.status_code == 400

    def test_metrics_exceeds_max_range_returns_400(self, client):
        """Requests wider than METRICS_LIST_MAX_RANGE_DAYS must return 400."""
        response = client.get("/api/v1/metrics?last=90d")
        assert response.status_code == 400

    def test_metrics_within_max_range_returns_200(self, client, mock_combined_metrics_repo):
        """Requests within METRICS_LIST_MAX_RANGE_DAYS should pass."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[])
        response = client.get("/api/v1/metrics?last=7d")
        assert response.status_code == 200

    def test_metrics_invalid_last_returns_400(self, client):
        """Should return 400 for invalid last parameter."""
        response = client.get("/api/v1/metrics?last=invalid")
        assert response.status_code == 400

    def test_metrics_contains_expected_fields(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Each metric should contain the expected fields."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics")
        data = response.json()
        metric = data["items"][0]
        expected_fields = [
            "pod_name",
            "namespace",
            "total_cost",
            "co2e_grams",
            "pue",
            "grid_intensity",
            "joules",
            "cpu_request",
            "memory_request",
            "embodied_co2e_grams",
        ]
        for field in expected_fields:
            assert field in metric, f"Missing field: {field}"


class TestMetricsSummaryEndpoint:
    """Tests for GET /api/v1/metrics/summary."""

    def test_summary_returns_200(self, client):
        """Should return 200 with empty summary."""
        response = client.get("/api/v1/metrics/summary")
        assert response.status_code == 200

    def test_summary_with_no_data(self, client):
        """Should return zeroed summary when no data exists."""
        response = client.get("/api/v1/metrics/summary")
        data = response.json()
        assert data["total_co2e_grams"] == 0.0
        assert data["total_cost"] == 0.0
        assert data["total_energy_joules"] == 0.0
        assert data["total_embodied_co2e_grams"] == 0.0
        assert data["total_co2e_all_scopes"] == 0.0
        assert data["pod_count"] == 0
        assert data["namespace_count"] == 0

    def test_summary_with_data(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should return correctly aggregated summary."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary")
        data = response.json()
        assert data["total_co2e_grams"] == pytest.approx(5.7)
        assert data["total_cost"] == pytest.approx(0.017)
        assert data["total_energy_joules"] == pytest.approx(20000.0)
        assert data["total_embodied_co2e_grams"] == pytest.approx(0.17)
        assert data["total_co2e_all_scopes"] == pytest.approx(5.7 + 0.17)
        assert data["pod_count"] == 2
        assert data["namespace_count"] == 2

    def test_summary_filter_by_namespace(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should filter summary by namespace."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary?namespace=default")
        data = response.json()
        assert data["pod_count"] == 1
        assert data["total_co2e_grams"] == pytest.approx(1.5)

    def test_summary_filter_by_last(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should accept last time filter."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary?last=24h")
        assert response.status_code == 200

    def test_summary_supports_ytd_last(self, client, mock_combined_metrics_repo):
        """YTD should resolve to January 1st of the current UTC year."""
        mock_combined_metrics_repo.aggregate_summary = AsyncMock(return_value={})

        response = client.get("/api/v1/metrics/summary?last=ytd")

        assert response.status_code == 200
        assert mock_combined_metrics_repo.aggregate_summary.await_args is not None
        kwargs = mock_combined_metrics_repo.aggregate_summary.await_args.kwargs
        start = kwargs["start_time"]
        end = kwargs["end_time"]
        assert start.year == end.year
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.tzinfo is not None


class TestMetricsByNamespaceEndpoint:
    """Tests for GET /api/v1/metrics/by-namespace."""

    def test_by_namespace_returns_200(self, client):
        """Should return 200 with an empty list when no data."""
        response = client.get("/api/v1/metrics/by-namespace")
        assert response.status_code == 200
        assert response.json() == []

    def test_by_namespace_returns_aggregated_rows(self, client, mock_combined_metrics_repo):
        """Should surface the aggregate_by_namespace repo rows as NamespaceBreakdownItems."""
        mock_combined_metrics_repo.aggregate_by_namespace = AsyncMock(
            return_value=[
                {
                    "namespace": "prod",
                    "co2e_grams": 42.0,
                    "embodied_co2e_grams": 4.2,
                    "total_cost": 1.5,
                    "energy_joules": 5000.0,
                }
            ]
        )
        response = client.get("/api/v1/metrics/by-namespace?last=7d")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["namespace"] == "prod"
        assert data[0]["co2e_grams"] == 42.0

    def test_by_namespace_invalid_last_returns_400(self, client):
        response = client.get("/api/v1/metrics/by-namespace?last=bad_value")
        assert response.status_code == 400


class TestMetricsTopPodsEndpoint:
    """Tests for GET /api/v1/metrics/top-pods."""

    def test_top_pods_returns_200(self, client):
        """Should return 200 with an empty list when no data."""
        response = client.get("/api/v1/metrics/top-pods")
        assert response.status_code == 200
        assert response.json() == []

    def test_top_pods_returns_ranked_rows(self, client, mock_combined_metrics_repo):
        """Should surface the aggregate_top_pods repo rows as TopPodItems."""
        mock_combined_metrics_repo.aggregate_top_pods = AsyncMock(
            return_value=[
                {
                    "namespace": "prod",
                    "pod_name": "heavy-api",
                    "co2e_grams": 100.0,
                    "embodied_co2e_grams": 10.0,
                    "total_cost": 5.0,
                    "energy_joules": 20000.0,
                }
            ]
        )
        response = client.get("/api/v1/metrics/top-pods?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["pod_name"] == "heavy-api"
        assert data[0]["co2e_grams"] == 100.0

    def test_top_pods_limit_too_large_returns_422(self, client):
        """Limit > 50 should be rejected by FastAPI validation."""
        response = client.get("/api/v1/metrics/top-pods?limit=100")
        assert response.status_code == 422

    def test_top_pods_invalid_last_returns_400(self, client):
        response = client.get("/api/v1/metrics/top-pods?last=bad_value")
        assert response.status_code == 400
