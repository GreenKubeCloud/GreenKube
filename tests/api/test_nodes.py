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


class TestNodesGpuSource:
    """GET /api/v1/nodes exposes where each node's GPU power came from (NRG-13 badge)."""

    def test_nodes_carry_the_gpu_source_of_their_latest_idle_row(
        self, client, mock_node_repo, mock_combined_metrics_repo, sample_node_infos
    ):
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        mock_combined_metrics_repo.read_latest_idle_gpu_sources = AsyncMock(
            return_value={"node-1": "gpu_tdp_estimate", "node-2": None}
        )
        data = client.get("/api/v1/nodes").json()
        by_name = {node["name"]: node for node in data}
        assert by_name["node-1"]["gpu_source"] == "gpu_tdp_estimate"
        assert by_name["node-2"]["gpu_source"] is None

    def test_paginated_nodes_carry_the_gpu_source(
        self, client, mock_node_repo, mock_combined_metrics_repo, sample_node_infos
    ):
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        mock_combined_metrics_repo.read_latest_idle_gpu_sources = AsyncMock(return_value={"node-2": "unmeasured"})
        data = client.get("/api/v1/nodes", params={"limit": 10}).json()
        assert {node["name"]: node["gpu_source"] for node in data["items"]} == {"node-1": None, "node-2": "unmeasured"}

    def test_unreadable_idle_rows_leave_the_gpu_source_unknown(
        self, client, mock_node_repo, mock_combined_metrics_repo, sample_node_infos
    ):
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        mock_combined_metrics_repo.read_latest_idle_gpu_sources = AsyncMock(side_effect=RuntimeError("db down"))
        response = client.get("/api/v1/nodes")
        assert response.status_code == 200
        assert all(node["gpu_source"] is None for node in response.json())
