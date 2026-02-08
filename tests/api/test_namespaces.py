# tests/api/test_namespaces.py
"""Tests for the namespaces endpoint."""

from unittest.mock import AsyncMock

from greenkube.models.metrics import CombinedMetric


class TestNamespacesEndpoint:
    """Tests for GET /api/v1/namespaces."""

    def test_namespaces_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/namespaces")
        assert response.status_code == 200

    def test_namespaces_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/namespaces")
        data = response.json()
        assert data == []

    def test_namespaces_returns_unique_namespaces(self, client, mock_carbon_repo, sample_combined_metrics):
        """Should return a sorted unique list of namespaces."""
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/namespaces")
        data = response.json()
        assert data == ["default", "production"]

    def test_namespaces_are_sorted(self, client, mock_carbon_repo):
        """Should return namespaces in alphabetical order."""
        from datetime import datetime, timezone

        metrics = [
            CombinedMetric(
                pod_name="p1",
                namespace="z-namespace",
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            ),
            CombinedMetric(
                pod_name="p2",
                namespace="a-namespace",
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            ),
            CombinedMetric(
                pod_name="p3",
                namespace="m-namespace",
                timestamp=datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc),
            ),
        ]
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/namespaces")
        data = response.json()
        assert data == ["a-namespace", "m-namespace", "z-namespace"]
