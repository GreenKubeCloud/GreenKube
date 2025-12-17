from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.postgres_node_repository import PostgresNodeRepository


@pytest.fixture
def connection_mock():
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.executemany.return_value = None
    return conn


@pytest.fixture
def mock_db_manager(connection_mock):
    manager = MagicMock()

    @asynccontextmanager
    async def conn_scope():
        yield connection_mock

    manager.connection_scope = conn_scope
    return manager


@pytest.fixture
def repository(mock_db_manager):
    return PostgresNodeRepository(mock_db_manager)


@pytest.mark.asyncio
async def test_save_nodes_success(repository, connection_mock):
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
    count = await repository.save_nodes(nodes)

    # Verify
    assert count == 1
    connection_mock.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_get_snapshots_success(repository, connection_mock):
    # Setup
    start = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 1, 1, 23, 59, tzinfo=timezone.utc)

    db_row = {
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

    connection_mock.fetch.return_value = [db_row]

    # Execute
    snapshots = await repository.get_snapshots(start, end)

    # Verify
    assert len(snapshots) == 1
    timestamp_str, node = snapshots[0]

    # Check values
    assert timestamp_str == datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    assert isinstance(node, NodeInfo)
    assert node.name == "node1"


@pytest.mark.asyncio
async def test_get_latest_snapshots_before_success(repository, connection_mock):
    # Setup
    cutoff = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)

    db_row = {
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

    connection_mock.fetch.return_value = [db_row]

    # Execute
    nodes = await repository.get_latest_snapshots_before(cutoff)

    # Verify
    assert len(nodes) == 1
    node = nodes[0]
    assert isinstance(node, NodeInfo)
    assert node.name == "node1"
