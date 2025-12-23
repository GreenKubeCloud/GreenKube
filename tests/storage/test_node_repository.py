# tests/storage/test_node_repository.py

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from greenkube.core.exceptions import QueryError
from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite_node_repository import SQLiteNodeRepository

# --- Fixtures ---


@pytest.fixture
async def db_connection():
    """Creates an in-memory SQLite database connection for testing."""
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute("""
            CREATE TABLE node_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                node_name TEXT NOT NULL,
                instance_type TEXT,
                cpu_capacity_cores REAL,
                architecture TEXT,
                cloud_provider TEXT,
                region TEXT,
                zone TEXT,
                node_pool TEXT,
                memory_capacity_bytes INTEGER,
                embodied_emissions_kg REAL,
                UNIQUE(node_name, timestamp)
            );
        """)
        await conn.commit()
        yield conn


@pytest.fixture
async def mock_db_manager(db_connection):
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_connection

    db_manager.connection_scope = scope
    return db_manager


@pytest.fixture
async def node_repo(mock_db_manager):
    """Creates an instance of the NodeRepository."""
    return SQLiteNodeRepository(mock_db_manager)


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
    NodeInfo(
        name="node-2",
        instance_type="t3.medium",
        zone="us-east-1b",
        region="us-east-1",
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=4294967296,
    ),
]


# --- Test Cases ---


@pytest.mark.asyncio
async def test_save_nodes_new_snapshots(node_repo, db_connection):
    """Test saving multiple new node snapshots successfully."""
    # Act
    saved_count = await node_repo.save_nodes(SAMPLE_NODES)

    # Assert
    assert saved_count == 2

    # Verify data in DB
    async with db_connection.execute("SELECT COUNT(*) FROM node_snapshots") as cursor:
        row = await cursor.fetchone()
        assert row[0] == 2

    async with db_connection.execute(
        "SELECT instance_type, cpu_capacity_cores FROM node_snapshots WHERE node_name=?", ("node-1",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == "m5.large"
        assert row[1] == 2.0


@pytest.mark.asyncio
async def test_get_snapshots(node_repo):
    """Test retrieving snapshots within a time range."""
    # Insert some data
    node1 = NodeInfo(
        name="node-1",
        instance_type="t3.medium",
        zone="us-east-1a",
        region="us-east-1",
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=4000000000,
    )
    await node_repo.save_nodes([node1])

    # Wait a bit or mock time? save_nodes uses datetime.now(timezone.utc).
    # Since we can't easily mock datetime inside the method without patching,
    # let's just use a wide range.

    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    end = datetime.now(timezone.utc) + timedelta(minutes=1)

    snapshots = await node_repo.get_snapshots(start, end)
    assert len(snapshots) == 1
    ts, info = snapshots[0]
    assert info.name == "node-1"
    assert info.cpu_capacity_cores == 2.0


@pytest.mark.asyncio
async def test_get_latest_snapshots_before(node_repo, db_connection):
    """Test retrieving latest snapshots before a timestamp."""
    # We need to simulate history.
    # Since save_nodes uses current time, we might need to manually insert for testing history
    # or patch datetime.

    # Insert snapshot at T-10m
    t1 = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await db_connection.execute(
        """
        INSERT INTO node_snapshots (
            timestamp, node_name, instance_type, cpu_capacity_cores, architecture,
            cloud_provider, region, zone, node_pool, memory_capacity_bytes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (t1, "node-1", "t3.medium", 2.0, "amd64", "aws", "us-east-1", "us-east-1a", "default", 4000000000),
    )

    # Insert snapshot at T-5m (updated capacity)
    t2 = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db_connection.execute(
        """
        INSERT INTO node_snapshots (
            timestamp, node_name, instance_type, cpu_capacity_cores, architecture,
            cloud_provider, region, zone, node_pool, memory_capacity_bytes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (t2, "node-1", "t3.large", 4.0, "amd64", "aws", "us-east-1", "us-east-1a", "default", 8000000000),
    )

    await db_connection.commit()

    # Query at T-2m (should get T-5m snapshot)
    query_time = datetime.now(timezone.utc) - timedelta(minutes=2)
    snapshots = await node_repo.get_latest_snapshots_before(query_time)

    assert len(snapshots) == 1
    assert snapshots[0].name == "node-1"
    assert snapshots[0].cpu_capacity_cores == 4.0

    # Query at T-7m (should get T-10m snapshot)
    query_time_old = datetime.now(timezone.utc) - timedelta(minutes=7)
    snapshots_old = await node_repo.get_latest_snapshots_before(query_time_old)

    assert len(snapshots_old) == 1
    assert snapshots_old[0].name == "node-1"
    assert snapshots_old[0].cpu_capacity_cores == 2.0


@pytest.mark.asyncio
async def test_save_nodes_multiple_snapshots(node_repo, db_connection):
    """Test saving multiple snapshots over time."""
    # Arrange & Act

    # Let's patch datetime in the module
    from unittest.mock import patch

    with patch("greenkube.storage.sqlite_node_repository.datetime") as mock_datetime:
        # First save
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        await node_repo.save_nodes(SAMPLE_NODES)

        # Second save (1 hour later)
        mock_datetime.now.return_value = datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

        updated_node = NodeInfo(
            name="node-1",
            instance_type="m5.xlarge",  # Changed
            zone="us-east-1a",
            region="us-east-1",
            cloud_provider="aws",
            architecture="amd64",
            node_pool="default",
            cpu_capacity_cores=4.0,
            memory_capacity_bytes=17179869184,
        )
        saved_count = await node_repo.save_nodes([updated_node])

    # Assert
    assert saved_count == 1

    # Verify total snapshots
    async with db_connection.execute("SELECT COUNT(*) FROM node_snapshots WHERE node_name=?", ("node-1",)) as cursor:
        row = await cursor.fetchone()
        assert row[0] == 2

    # Verify latest snapshot
    async with db_connection.execute(
        "SELECT instance_type FROM node_snapshots WHERE node_name=? ORDER BY timestamp DESC LIMIT 1", ("node-1",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == "m5.xlarge"


@pytest.mark.asyncio
async def test_save_nodes_empty_list(node_repo):
    """Test saving an empty list of nodes."""
    saved_count = await node_repo.save_nodes([])
    assert saved_count == 0


@pytest.mark.asyncio
async def test_save_nodes_db_error():
    """Test behavior when DB raises an error."""
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        mock_conn = MagicMock()

        async def mock_executemany(*args, **kwargs):
            raise aiosqlite.Error("DB Error")

        mock_conn.executemany = AsyncMock(side_effect=mock_executemany)
        yield mock_conn

    db_manager.connection_scope = scope

    repo = SQLiteNodeRepository(db_manager)
    with pytest.raises(QueryError):
        await repo.save_nodes(SAMPLE_NODES)
