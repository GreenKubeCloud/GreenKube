# tests/storage/test_elasticsearch_node_repository.py

from datetime import datetime, timezone
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


def test_get_snapshots(es_node_repo, mock_connections):
    """Test retrieving snapshots."""
    # Mock search
    mock_search = MagicMock()
    with patch("greenkube.storage.elasticsearch_node_repository.NodeSnapshotDoc.search", return_value=mock_search):
        mock_search.filter.return_value.sort.return_value.scan.return_value = [
            MagicMock(
                timestamp="2023-01-01T12:00:00+00:00",
                node_name="node-1",
                instance_type="t3.medium",
                cpu_capacity_cores=2.0,
                memory_capacity_bytes=4000000000,
                zone="us-east-1a",
                region="us-east-1",
                cloud_provider="aws",
                architecture="amd64",
                node_pool="default",
            )
        ]

        start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2023, 1, 1, 14, 0, 0, tzinfo=timezone.utc)

        snapshots = es_node_repo.get_snapshots(start, end)
        assert len(snapshots) == 1
        ts, info = snapshots[0]
        assert ts == "2023-01-01T12:00:00+00:00"
        assert info.name == "node-1"


def test_get_latest_snapshots_before(es_node_repo, mock_connections):
    """Test retrieving latest snapshots before timestamp."""
    # Mock search and aggregation response
    mock_search = MagicMock()
    with patch("greenkube.storage.elasticsearch_node_repository.NodeSnapshotDoc.search", return_value=mock_search):
        mock_response = MagicMock()
        # s.execute() is called on the search object returned by filter()
        mock_search.filter.return_value.execute.return_value = mock_response

        # Ensure aggs.bucket().metric() chain doesn't crash
        mock_search.filter.return_value.aggs.bucket.return_value.metric.return_value = MagicMock()

        # Mock aggregation buckets
        mock_bucket = MagicMock()
        mock_bucket.latest_snapshot.hits.hits = [
            {
                "_source": {
                    "node_name": "node-1",
                    "instance_type": "t3.medium",
                    "cpu_capacity_cores": 2.0,
                    "memory_capacity_bytes": 4000000000,
                    "zone": "us-east-1a",
                    "region": "us-east-1",
                    "cloud_provider": "aws",
                    "architecture": "amd64",
                    "node_pool": "default",
                    "timestamp": "2023-01-01T11:00:00+00:00",
                }
            }
        ]
        mock_response.aggregations.nodes.buckets = [mock_bucket]

        ts = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        snapshots = es_node_repo.get_latest_snapshots_before(ts)

        assert len(snapshots) == 1
        assert snapshots[0].name == "node-1"
        assert snapshots[0].cpu_capacity_cores == 2.0
