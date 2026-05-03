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

    def test_nodes_excludes_inactive_by_default(self, client, mock_node_repo):
        """The route should request only active nodes unless include_inactive is set."""
        response = client.get("/api/v1/nodes")

        assert response.status_code == 200
        mock_node_repo.get_latest_snapshots_before.assert_awaited_once()
        _, kwargs = mock_node_repo.get_latest_snapshots_before.await_args
        assert kwargs["include_inactive"] is False

    def test_nodes_can_include_inactive(self, client, mock_node_repo):
        """The route should pass through include_inactive=true to the repository."""
        response = client.get("/api/v1/nodes", params={"include_inactive": "true"})

        assert response.status_code == 200
        mock_node_repo.get_latest_snapshots_before.assert_awaited_once()
        _, kwargs = mock_node_repo.get_latest_snapshots_before.await_args
        assert kwargs["include_inactive"] is True

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
            "is_active",
        ]
        for field in expected_fields:
            assert field in node, f"Missing field: {field}"
