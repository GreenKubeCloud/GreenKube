from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.postgres_node_repository import PostgresNodeRepository


@pytest.fixture
def mock_db_manager():
    manager = MagicMock()
    # Mock connection_scope context manager
    connection = MagicMock()
    manager.connection_scope.return_value.__enter__.return_value = connection

    # Mock cursor context manager
    cursor_ctx = MagicMock()
    real_cursor = MagicMock()
    connection.cursor.return_value = cursor_ctx
    cursor_ctx.__enter__.return_value = real_cursor

    return manager, real_cursor


@pytest.fixture
def repository(mock_db_manager):
    manager, _ = mock_db_manager
    return PostgresNodeRepository(manager)


def test_save_nodes_success(repository, mock_db_manager):
    manager, cursor = mock_db_manager
    # Setup
    node = NodeInfo(
        name="node1",
        instance_type="t3.medium",
        cpu_capacity_cores=2.0,
        architecture="x86_64",
        cloud_provider="aws",
        region="eu-west-1",
        zone="eu-west-1a",
        node_pool="default",
        memory_capacity_bytes=4096,
        timestamp=datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    nodes = [node]

    # Execute
    count = repository.save_nodes(nodes)

    # Verify
    assert count == 1
    cursor.executemany.assert_called_once()

    # Check that 'name' was mapped to 'node_name'
    call_args = cursor.executemany.call_args[0]
    inserted_data = call_args[1]
    assert len(inserted_data) == 1
    assert "node_name" in inserted_data[0]
    assert inserted_data[0]["node_name"] == "node1"
    assert "name" not in inserted_data[0]

    conn = manager.connection_scope.return_value.__enter__.return_value
    conn.commit.assert_called_once()


def test_get_snapshots_success(repository, mock_db_manager):
    _, cursor = mock_db_manager
    # Setup
    start = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 1, 1, 23, 59, tzinfo=timezone.utc)

    db_row = {
        "id": 1,
        "node_name": "node1",
        "instance_type": "t3.medium",
        "cpu_capacity_cores": 2.0,
        "architecture": "x86_64",
        "cloud_provider": "aws",
        "region": "eu-west-1",
        "zone": "eu-west-1a",
        "node_pool": "default",
        "memory_capacity_bytes": 4096,
        "timestamp": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
    }

    cursor.fetchall.return_value = [db_row]

    # Execute
    snapshots = repository.get_snapshots(start, end)

    # Verify
    assert len(snapshots) == 1
    timestamp_str, node = snapshots[0]
    # db_row was modified in place (pop), so we use the original value
    assert timestamp_str == datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    assert isinstance(node, NodeInfo)
    assert node.name == "node1"


def test_get_latest_snapshots_before_success(repository, mock_db_manager):
    _, cursor = mock_db_manager
    # Setup
    cutoff = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)

    db_row = {
        "id": 1,
        "node_name": "node1",
        "instance_type": "t3.medium",
        "cpu_capacity_cores": 2.0,
        "architecture": "x86_64",
        "cloud_provider": "aws",
        "region": "eu-west-1",
        "zone": "eu-west-1a",
        "node_pool": "default",
        "memory_capacity_bytes": 4096,
        "timestamp": datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
    }

    cursor.fetchall.return_value = [db_row]

    # Execute
    nodes = repository.get_latest_snapshots_before(cutoff)

    # Verify
    assert len(nodes) == 1
    node = nodes[0]
    assert isinstance(node, NodeInfo)
    assert node.name == "node1"
