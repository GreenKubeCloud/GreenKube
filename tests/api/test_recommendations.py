# tests/api/test_recommendations.py
"""Tests for the recommendations endpoint."""

from unittest.mock import AsyncMock


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

    def test_recommendations_with_zombie_pod(self, client, mock_carbon_repo):
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
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=[zombie_metric])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        assert len(data) >= 1
        assert any(r["type"] == "ZOMBIE_POD" for r in data)

    def test_recommendations_filter_by_namespace(self, client, mock_carbon_repo):
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
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/recommendations?namespace=team-a")
        data = response.json()
        assert all(r["namespace"] == "team-a" for r in data)

    def test_recommendations_contain_expected_fields(self, client, mock_carbon_repo):
        """Each recommendation should contain expected fields."""
        from datetime import datetime, timezone

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
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=[zombie])
        response = client.get("/api/v1/recommendations")
        data = response.json()
        if data:
            rec = data[0]
            for field in ["pod_name", "namespace", "type", "description"]:
                assert field in rec, f"Missing field: {field}"
