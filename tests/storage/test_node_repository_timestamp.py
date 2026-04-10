from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite_node_repository import SQLiteNodeRepository


async def _create_test_db():
    """Create an in-memory SQLite database with the required tables."""
    conn = await aiosqlite.connect(":memory:")
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
    await conn.execute("""
        CREATE TABLE node_snapshots_scd (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            is_current BOOLEAN NOT NULL DEFAULT 1
        );
    """)
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_save_nodes_uses_node_timestamp():
    """SCD2 save_nodes should use the node's timestamp for valid_from."""
    conn = await _create_test_db()
    try:
        mock_db_manager = MagicMock()

        @asynccontextmanager
        async def scope():
            yield conn

        mock_db_manager.connection_scope = scope
        repo = SQLiteNodeRepository(mock_db_manager)

        specific_ts = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        node = NodeInfo(
            name="test-node",
            instance_type="m5.large",
            zone="us-east-1a",
            region="us-east-1",
            cloud_provider="aws",
            architecture="amd64",
            node_pool="default",
            cpu_capacity_cores=2,
            memory_capacity_bytes=8589934592,
            timestamp=specific_ts,
        )

        saved = await repo.save_nodes([node])
        assert saved == 1

        # Check SCD record uses the specific timestamp
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM node_snapshots_scd WHERE node_name = ?", ("test-node",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["valid_from"] == specific_ts.isoformat()
        assert row["is_current"] == 1

        # Legacy table should also have the specific timestamp
        async with conn.execute("SELECT * FROM node_snapshots WHERE node_name = ?", ("test-node",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["timestamp"] == specific_ts.isoformat()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_save_nodes_uses_current_time_if_none():
    """SCD2 save_nodes should use current time when node.timestamp is None."""
    conn = await _create_test_db()
    try:
        mock_db_manager = MagicMock()

        @asynccontextmanager
        async def scope():
            yield conn

        mock_db_manager.connection_scope = scope
        repo = SQLiteNodeRepository(mock_db_manager)

        node = NodeInfo(
            name="test-node",
            instance_type="m5.large",
            zone="us-east-1a",
            region="us-east-1",
            cloud_provider="aws",
            architecture="amd64",
            node_pool="default",
            cpu_capacity_cores=2,
            memory_capacity_bytes=8589934592,
            timestamp=None,
        )

        saved = await repo.save_nodes([node])
        assert saved == 1

        # Check SCD record has a valid ISO timestamp
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT valid_from FROM node_snapshots_scd WHERE node_name = ?", ("test-node",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        dt = datetime.fromisoformat(row["valid_from"])
        assert dt is not None
    finally:
        await conn.close()
