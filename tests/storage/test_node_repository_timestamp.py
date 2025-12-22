from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite_node_repository import SQLiteNodeRepository


@pytest.mark.asyncio
async def test_save_nodes_uses_node_timestamp():
    # Arrange
    mock_db_manager = MagicMock()
    mock_conn = MagicMock()

    # Mock cursor context manager
    mock_cursor = MagicMock()
    # execute, fetchone, etc should be awaitable
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock()

    async def mock_execute(*args, **kwargs):
        return mock_cursor

    mock_conn.execute = AsyncMock(side_effect=mock_execute)
    mock_conn.commit = AsyncMock()

    @asynccontextmanager
    async def scope():
        yield mock_conn

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

    # Act
    await repo.save_nodes([node])

    # Assert
    # Check that execute was called with the specific timestamp
    # In async loop, we are calling `async with conn.execute(...) as cursor:`
    # So `conn.execute` is called.
    args, _ = mock_conn.execute.call_args
    query, params = args

    # The first parameter should be the timestamp string
    inserted_ts = params[0]
    assert inserted_ts == specific_ts.isoformat()


@pytest.mark.asyncio
async def test_save_nodes_uses_current_time_if_none():
    # Arrange
    mock_db_manager = MagicMock()
    mock_conn = MagicMock()

    # Mock cursor context manager - actually in SQLiteNodeRepository we use `async with conn.execute(...)`
    # so conn.execute is the context manager entry point presumably?
    # Or calls `execute` on connection directly which returns an awaitable cursor or context manager.
    # aiosqlite `conn.execute()` returns a context manager that upon enter yields cursor.

    # Let's mock `conn.execute` to return an async context manager that yields a cursor
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1

    @asynccontextmanager
    async def mock_execute_cm(*args, **kwargs):
        yield mock_cursor

    mock_conn.execute = MagicMock(side_effect=mock_execute_cm)
    mock_conn.commit = AsyncMock()

    @asynccontextmanager
    async def scope():
        yield mock_conn

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

    # Act
    await repo.save_nodes([node])

    # Assert
    args, _ = mock_conn.execute.call_args
    query, params = args

    # The first parameter should be a timestamp string (current time)
    inserted_ts = params[0]
    assert isinstance(inserted_ts, str)
    # Should be recent
    dt = datetime.fromisoformat(inserted_ts)
    assert dt is not None
