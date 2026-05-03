# tests/api/test_recommendation_history.py
"""
Tests for the recommendation history API endpoint.
TDD: Tests written before implementation.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from greenkube.models.metrics import (
    CombinedMetric,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)


class TestRecommendationHistoryEndpoint:
    """Tests for GET /api/v1/recommendations/history."""

    def test_history_returns_200(self, client):
        """Should return 200 with valid params."""
        response = client.get(
            "/api/v1/recommendations/history",
            params={"start": "2026-02-01T00:00:00Z", "end": "2026-02-28T00:00:00Z"},
        )
        assert response.status_code == 200

    def test_history_returns_empty_list_when_no_records(self, client):
        """Should return an empty list when no history exists."""
        response = client.get(
            "/api/v1/recommendations/history",
            params={"start": "2026-02-01T00:00:00Z", "end": "2026-02-28T00:00:00Z"},
        )
        assert response.json() == []

    def test_history_with_type_filter(self, client, mock_reco_repo):
        """Should pass the type filter to the repository."""
        records = [
            RecommendationRecord(
                pod_name="zombie-pod",
                namespace="default",
                type=RecommendationType.ZOMBIE_POD,
                description="Test zombie",
                created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            )
        ]
        mock_reco_repo.get_recommendations = AsyncMock(return_value=records)
        response = client.get(
            "/api/v1/recommendations/history",
            params={
                "start": "2026-02-01T00:00:00Z",
                "end": "2026-02-28T00:00:00Z",
                "type": "ZOMBIE_POD",
            },
        )
        data = response.json()
        assert len(data) == 1
        assert data[0]["type"] == "ZOMBIE_POD"

    def test_history_with_namespace_filter(self, client, mock_reco_repo):
        """Should pass the namespace filter to the repository."""
        records = [
            RecommendationRecord(
                pod_name="cpu-pod",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="Oversized CPU",
                created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            )
        ]
        mock_reco_repo.get_recommendations = AsyncMock(return_value=records)
        response = client.get(
            "/api/v1/recommendations/history",
            params={
                "start": "2026-02-01T00:00:00Z",
                "end": "2026-02-28T00:00:00Z",
                "namespace": "prod",
            },
        )
        data = response.json()
        assert len(data) == 1
        assert data[0]["namespace"] == "prod"


class TestRecommendationsPersistence:
    """Tests that recommendations are persisted when generated."""

    def test_recommendations_are_saved_on_generation(self, client, mock_combined_metrics_repo, mock_reco_repo):
        """Generating recommendations should also persist them via upsert."""
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
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        # The upsert_recommendations method should have been called
        mock_reco_repo.upsert_recommendations.assert_called_once()
        saved_records = mock_reco_repo.upsert_recommendations.call_args[0][0]
        assert len(saved_records) >= 1
        assert all(isinstance(r, RecommendationRecord) for r in saved_records)
        mock_reco_repo.reconcile_active_recommendations.assert_called_once()

    def test_active_endpoint_refreshes_recommendations_when_requested(
        self,
        client,
        mock_combined_metrics_repo,
        mock_reco_repo,
    ):
        """The active endpoint should refresh/reconcile before returning when requested by the UI."""
        metric = CombinedMetric(
            pod_name="api-7f9c5d9f6d-a1b2c",
            namespace="prod",
            total_cost=0.05,
            co2e_grams=1.0,
            joules=100.0,
            cpu_request=1000,
            memory_request=512 * 1024 * 1024,
            cpu_usage_millicores=20,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        active_record = RecommendationRecord(
            scope="workload",
            pod_name="api",
            namespace="prod",
            type=RecommendationType.RIGHTSIZING_CPU,
            status=RecommendationStatus.ACTIVE,
            description="Deployment 'api' is oversized",
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[metric])
        mock_reco_repo.get_active_recommendations = AsyncMock(return_value=[active_record])

        response = client.get("/api/v1/recommendations/active", params={"namespace": "prod", "refresh": "true"})

        assert response.status_code == 200
        data = response.json()
        assert data[0]["scope"] == "workload"
        assert data[0]["pod_name"] == "api"
        mock_reco_repo.upsert_recommendations.assert_called_once()
        mock_reco_repo.reconcile_active_recommendations.assert_called_once()
        mock_reco_repo.get_active_recommendations.assert_called_once_with(namespace="prod")

    def test_metrics_from_deleted_namespaces_are_excluded_from_recommendations(
        self,
        client,
        mock_combined_metrics_repo,
        mock_reco_repo,
    ):
        """Metrics for a deleted Kubernetes namespace must not produce or keep active recommendations."""
        # The DB still holds historical data from the now-deleted namespace.
        deleted_ns_metric = CombinedMetric(
            pod_name="api-7f9c5d9f6d-a1b2c",
            namespace="greenkube-db-testing",
            total_cost=0.05,
            co2e_grams=1.0,
            joules=100.0,
            cpu_request=1000,
            memory_request=512 * 1024 * 1024,
            cpu_usage_millicores=20,
            timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            duration_seconds=300,
        )
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[deleted_ns_metric])

        # Kubernetes only knows about "greenkube" and "default" — NOT "greenkube-db-testing".
        with patch(
            "greenkube.api.routers.recommendations._get_active_k8s_namespaces",
            new=AsyncMock(return_value={"greenkube", "default"}),
        ):
            response = client.get("/api/v1/recommendations/active", params={"refresh": "true"})

        assert response.status_code == 200
        # All metrics were filtered out → reconcile is called with an empty list,
        # which will mark the greenkube-db-testing rows as stale.
        mock_reco_repo.reconcile_active_recommendations.assert_called_once_with([], namespace=None)
        # No recommendations were generated for the deleted namespace.
        mock_reco_repo.upsert_recommendations.assert_not_called()
