# tests/api/test_dashboard_summary.py
"""Tests for the pre-computed dashboard summary endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    get_summary_repository,
)
from greenkube.models.metrics import MetricsSummaryRow


@pytest.fixture
def mock_summary_repo():
    """Returns a mock SummaryRepository."""
    repo = AsyncMock()
    repo.get_rows = AsyncMock(return_value=[])
    repo.upsert_row = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def sample_summary_rows():
    """A small set of pre-computed summary rows."""
    now = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)
    return [
        MetricsSummaryRow(
            window_slug="24h",
            namespace=None,
            total_co2e_grams=120.5,
            total_embodied_co2e_grams=8.2,
            total_cost=1.42,
            total_energy_joules=432000.0,
            pod_count=12,
            namespace_count=3,
            updated_at=now,
        ),
        MetricsSummaryRow(
            window_slug="7d",
            namespace=None,
            total_co2e_grams=840.0,
            total_embodied_co2e_grams=57.4,
            total_cost=9.87,
            total_energy_joules=3024000.0,
            pod_count=14,
            namespace_count=3,
            updated_at=now,
        ),
    ]


@pytest.fixture
def client_with_summary(
    mock_carbon_repo,
    mock_combined_metrics_repo,
    mock_node_repo,
    mock_reco_repo,
    mock_summary_repo,
):
    """TestClient with all repository dependencies overridden."""
    app = create_app()
    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
    app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo
    app.dependency_overrides[get_summary_repository] = lambda: mock_summary_repo
    with TestClient(app) as c:
        yield c, mock_summary_repo
    app.dependency_overrides.clear()


class TestGetDashboardSummary:
    """Tests for GET /api/v1/metrics/dashboard-summary."""

    def test_returns_200_when_empty(self, client_with_summary):
        client, _ = client_with_summary
        response = client.get("/api/v1/metrics/dashboard-summary")
        assert response.status_code == 200
        data = response.json()
        assert data["windows"] == {}
        assert data["namespace"] is None

    def test_returns_windows_map(self, client_with_summary, sample_summary_rows):
        client, repo = client_with_summary
        repo.get_rows = AsyncMock(return_value=sample_summary_rows)

        response = client.get("/api/v1/metrics/dashboard-summary")
        assert response.status_code == 200
        data = response.json()

        assert "24h" in data["windows"]
        assert "7d" in data["windows"]
        assert data["windows"]["24h"]["total_co2e_grams"] == pytest.approx(120.5)
        assert data["windows"]["7d"]["total_cost"] == pytest.approx(9.87)

    def test_namespace_filter_passed_to_repo(self, client_with_summary):
        client, repo = client_with_summary
        client.get("/api/v1/metrics/dashboard-summary?namespace=production")
        repo.get_rows.assert_called_once_with(namespace="production")

    def test_invalid_namespace_returns_400(self, client_with_summary):
        client, _ = client_with_summary
        response = client.get("/api/v1/metrics/dashboard-summary?namespace=INVALID_NS!")
        assert response.status_code == 400


class TestRefreshDashboardSummary:
    """Tests for POST /api/v1/metrics/dashboard-summary/refresh."""

    def test_returns_202_accepted(self, client_with_summary):
        client, _ = client_with_summary
        response = client.post("/api/v1/metrics/dashboard-summary/refresh")
        assert response.status_code == 202
        data = response.json()
        assert "detail" in data

    def test_namespace_scoped_refresh(self, client_with_summary):
        client, _ = client_with_summary
        response = client.post("/api/v1/metrics/dashboard-summary/refresh?namespace=staging")
        assert response.status_code == 202

    def test_invalid_namespace_returns_400(self, client_with_summary):
        client, _ = client_with_summary
        response = client.post("/api/v1/metrics/dashboard-summary/refresh?namespace=INVALID_NS!")
        assert response.status_code == 400
