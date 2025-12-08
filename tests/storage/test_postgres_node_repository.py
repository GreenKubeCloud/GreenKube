from datetime import datetime
from unittest.mock import MagicMock

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.postgres_node_repository import PostgresNodeRepository


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = None
    return cursor


@pytest.fixture
def mock_connection(mock_cursor):
    connection = MagicMock()
    connection.cursor.return_value = mock_cursor
    return connection


@pytest.fixture
def mock_db_manager(mock_connection):
    db_manager = MagicMock()
    # Mock connection_scope as a context manager
    scope = MagicMock()
    scope.__enter__.return_value = mock_connection
    scope.__exit__.return_value = None
    db_manager.connection_scope.return_value = scope
    return db_manager


@pytest.fixture
def repository(mock_db_manager):
    return PostgresNodeRepository(mock_db_manager)


def test_save_nodes(repository, mock_cursor, mock_connection):
    node = NodeInfo(
        timestamp=datetime(2023, 1, 1, 12, 0, 0),
        name="node-1",
        instance_type="t3.medium",
        cpu_capacity_cores=2.0,
        architecture="x86_64",
        cloud_provider="aws",
        region="eu-west-1",
        zone="eu-west-1a",
        node_pool="default",
        memory_capacity_bytes=4096,
    )

    repository.save_nodes([node])

    mock_cursor.executemany.assert_called_once()
    mock_connection.commit.assert_called_once()


def test_get_snapshots(repository, mock_cursor):
    mock_cursor.fetchall.return_value = [
        {
            "timestamp": datetime(2023, 1, 1, 12, 0, 0),
            "node_name": "node-1",
            "instance_type": "t3.medium",
            "cpu_capacity_cores": 2.0,
            "architecture": "x86_64",
            "cloud_provider": "aws",
            "region": "eu-west-1",
            "zone": "eu-west-1a",
            "node_pool": "default",
            "memory_capacity_bytes": 4096,
        }
    ]

    snapshots = repository.get_snapshots(datetime(2023, 1, 1, 0, 0, 0), datetime(2023, 1, 2, 0, 0, 0))

    assert len(snapshots) == 1
    assert snapshots[0][1].name == "node-1"
    mock_cursor.execute.assert_called_once()


def test_get_latest_snapshots_before(repository, mock_cursor):
    mock_cursor.fetchall.return_value = [
        {
            "timestamp": datetime(2023, 1, 1, 12, 0, 0),
            "node_name": "node-1",
            "instance_type": "t3.medium",
            "cpu_capacity_cores": 2.0,
            "architecture": "x86_64",
            "cloud_provider": "aws",
            "region": "eu-west-1",
            "zone": "eu-west-1a",
            "node_pool": "default",
            "memory_capacity_bytes": 4096,
        }
    ]

    nodes = repository.get_latest_snapshots_before(datetime(2023, 1, 2, 0, 0, 0))

    assert len(nodes) == 1
    assert nodes[0].name == "node-1"
    mock_cursor.execute.assert_called_once()
