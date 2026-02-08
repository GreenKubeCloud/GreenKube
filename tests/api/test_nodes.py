# tests/api/test_nodes.py
"""Tests for the nodes endpoint."""

from unittest.mock import AsyncMock


class TestNodesEndpoint:
    """Tests for GET /api/v1/nodes."""

    def test_nodes_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/nodes")
        assert response.status_code == 200

    def test_nodes_returns_empty_list(self, client):
        """Should return an empty list when no nodes exist."""
        response = client.get("/api/v1/nodes")
        data = response.json()
        assert data == []

    def test_nodes_returns_data(self, client, mock_node_repo, sample_node_infos):
        """Should return node info from the repository."""
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        response = client.get("/api/v1/nodes")
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "node-1"
        assert data[1]["name"] == "node-2"

    def test_nodes_contain_expected_fields(self, client, mock_node_repo, sample_node_infos):
        """Each node should contain the expected fields."""
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        response = client.get("/api/v1/nodes")
        data = response.json()
        node = data[0]
        expected_fields = [
            "name",
            "instance_type",
            "zone",
            "region",
            "cloud_provider",
            "architecture",
            "cpu_capacity_cores",
            "memory_capacity_bytes",
        ]
        for field in expected_fields:
            assert field in node, f"Missing field: {field}"
