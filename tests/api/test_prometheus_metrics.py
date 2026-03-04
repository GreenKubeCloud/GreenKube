# tests/api/test_prometheus_metrics.py
"""
Tests for the Prometheus metrics exposition endpoint.
TDD: Tests written before implementation.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.models.metrics import (
    Recommendation,
    RecommendationType,
)


@pytest.fixture
def mock_carbon_repo():
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_combined_metrics_repo():
    repo = AsyncMock()
    repo.read_combined_metrics = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_node_repo():
    repo = AsyncMock()
    repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_reco_repo():
    repo = AsyncMock()
    repo.save_recommendations = AsyncMock(return_value=0)
    repo.get_recommendations = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def client(mock_carbon_repo, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
    app = create_app()
    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
    app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestMetricsEndpoint:
    """Tests for GET /prometheus/metrics."""

    def test_metrics_endpoint_returns_200(self, client):
        """Should return 200 OK."""
        response = client.get("/prometheus/metrics")
        assert response.status_code == 200

    def test_metrics_content_type_is_prometheus(self, client):
        """Should return Prometheus text format content type."""
        response = client.get("/prometheus/metrics")
        assert "text/plain" in response.headers.get("content-type", "")

    def test_metrics_contain_recommendation_gauges(self, client):
        """Should contain greenkube_recommendations_total gauge."""
        response = client.get("/prometheus/metrics")
        body = response.text
        assert "greenkube_recommendations_total" in body


class TestUpdateRecommendationMetrics:
    """Tests for the metrics update function."""

    def test_update_sets_gauge_values(self):
        """Should set gauge values based on recommendation list."""
        from greenkube.api.metrics_endpoint import update_recommendation_metrics

        recs = [
            Recommendation(
                pod_name="pod-1",
                namespace="default",
                type=RecommendationType.ZOMBIE_POD,
                description="Zombie",
                priority="high",
                potential_savings_cost=1.0,
                potential_savings_co2e_grams=0.5,
            ),
            Recommendation(
                pod_name="pod-2",
                namespace="default",
                type=RecommendationType.ZOMBIE_POD,
                description="Zombie 2",
                priority="high",
                potential_savings_cost=2.0,
                potential_savings_co2e_grams=1.0,
            ),
            Recommendation(
                pod_name="pod-3",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="Oversized CPU",
                priority="medium",
                potential_savings_cost=0.5,
                potential_savings_co2e_grams=0.2,
            ),
        ]
        update_recommendation_metrics(recs)

        from greenkube.api.metrics_endpoint import (
            RECOMMENDATION_COUNT,
            RECOMMENDATION_SAVINGS_COST,
        )

        # Check ZOMBIE_POD count
        zombie_count = RECOMMENDATION_COUNT.labels(type="ZOMBIE_POD", priority="high")._value.get()
        assert zombie_count == 2

        # Check RIGHTSIZING_CPU count
        cpu_count = RECOMMENDATION_COUNT.labels(type="RIGHTSIZING_CPU", priority="medium")._value.get()
        assert cpu_count == 1

        # Check cost savings
        zombie_cost = RECOMMENDATION_SAVINGS_COST.labels(type="ZOMBIE_POD")._value.get()
        assert zombie_cost == pytest.approx(3.0, abs=0.01)

        cpu_cost = RECOMMENDATION_SAVINGS_COST.labels(type="RIGHTSIZING_CPU")._value.get()
        assert cpu_cost == pytest.approx(0.5, abs=0.01)

    def test_update_with_empty_list_resets_gauges(self):
        """Should reset gauges when called with an empty list."""
        from greenkube.api.metrics_endpoint import (
            update_recommendation_metrics,
        )

        update_recommendation_metrics([])
        # After reset, all labeled gauges should be cleared
        # The gauge itself still exists but there should be no samples
