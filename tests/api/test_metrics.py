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
        assert data == []

    def test_metrics_returns_data(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should return metrics from the repository."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics")
        data = response.json()
        assert len(data) == 2
        assert data[0]["pod_name"] == "nginx-abc123"
        assert data[1]["pod_name"] == "api-server-xyz"

    def test_metrics_filter_by_namespace(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should filter metrics by namespace query param."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics?namespace=production")
        data = response.json()
        assert len(data) == 1
        assert data[0]["namespace"] == "production"

    def test_metrics_filter_by_last(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should accept --last style time filter."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics?last=7d")
        assert response.status_code == 200

    def test_metrics_invalid_last_returns_400(self, client):
        """Should return 400 for invalid last parameter."""
        response = client.get("/api/v1/metrics?last=invalid")
        assert response.status_code == 400

    def test_metrics_contains_expected_fields(self, client, mock_carbon_repo, sample_combined_metrics):
        """Each metric should contain the expected fields."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics")
        data = response.json()
        metric = data[0]
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
        assert data["pod_count"] == 0
        assert data["namespace_count"] == 0

    def test_summary_with_data(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should return correctly aggregated summary."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary")
        data = response.json()
        assert data["total_co2e_grams"] == pytest.approx(5.7)
        assert data["total_cost"] == pytest.approx(0.017)
        assert data["total_energy_joules"] == pytest.approx(20000.0)
        assert data["total_embodied_co2e_grams"] == pytest.approx(0.17)
        assert data["pod_count"] == 2
        assert data["namespace_count"] == 2

    def test_summary_filter_by_namespace(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should filter summary by namespace."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary?namespace=default")
        data = response.json()
        assert data["pod_count"] == 1
        assert data["total_co2e_grams"] == pytest.approx(1.5)

    def test_summary_filter_by_last(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should accept last time filter."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/metrics/summary?last=24h")
        assert response.status_code == 200
