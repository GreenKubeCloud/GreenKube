# tests/storage/test_elasticsearch_node_repository.py

from unittest.mock import MagicMock, patch

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.elasticsearch_node_repository import (
    ElasticsearchNodeRepository,
)

# --- Fixtures ---


@pytest.fixture
def mock_connections():
    with patch("greenkube.storage.elasticsearch_node_repository.connections") as mock:
        mock_conn = MagicMock()
        mock_conn.ping.return_value = True
        mock.get_connection.return_value = mock_conn
        yield mock


@pytest.fixture
def mock_bulk():
    with patch("greenkube.storage.elasticsearch_node_repository.bulk") as mock:
        mock.return_value = (1, [])
        yield mock


@pytest.fixture
def es_node_repo(mock_connections):
    return ElasticsearchNodeRepository()


# --- Sample Data ---

SAMPLE_NODES = [
    NodeInfo(
        name="node-1",
        instance_type="m5.large",
        zone="us-east-1a",
        region="us-east-1",
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=8589934592,
    ),
]

# --- Test Cases ---


def test_init(mock_connections):
    """Test initialization of ElasticsearchNodeRepository."""
    ElasticsearchNodeRepository()
    mock_connections.get_connection.assert_called_with("default")
    # NodeSnapshotDoc.init() calls get_connection internally usually,
    # but here we just check if connection was retrieved and pinged.
    mock_connections.get_connection.return_value.ping.assert_called_once()


def test_save_nodes_success(es_node_repo, mock_bulk, mock_connections):
    """Test saving nodes successfully."""
    saved_count = es_node_repo.save_nodes(SAMPLE_NODES)

    assert saved_count == 1
    mock_bulk.assert_called_once()

    # Verify bulk call arguments
    call_args = mock_bulk.call_args
    assert call_args.kwargs["client"] == mock_connections.get_connection.return_value
    actions = call_args.kwargs["actions"]
    assert len(actions) == 1
    assert actions[0]["_index"] == "greenkube_node_snapshots"
    assert actions[0]["_source"]["node_name"] == "node-1"
    assert actions[0]["_source"]["cpu_capacity_cores"] == 2.0


def test_save_nodes_empty(es_node_repo, mock_bulk):
    """Test saving empty list."""
    saved_count = es_node_repo.save_nodes([])
    assert saved_count == 0
    mock_bulk.assert_not_called()


def test_save_nodes_failure(es_node_repo, mock_bulk):
    """Test failure during save."""
    mock_bulk.side_effect = Exception("Bulk error")
    saved_count = es_node_repo.save_nodes(SAMPLE_NODES)
    assert saved_count == 0
