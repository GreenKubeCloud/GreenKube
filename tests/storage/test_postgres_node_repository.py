from datetime import datetime
from unittest.mock import MagicMock

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.postgres_node_repository import PostgresNodeRepository


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    return cursor


@pytest.fixture
def mock_connection(mock_cursor):
    connection = MagicMock()
    connection.cursor.return_value = mock_cursor
    return connection


@pytest.fixture
def repository(mock_connection):
    return PostgresNodeRepository(mock_connection)


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
