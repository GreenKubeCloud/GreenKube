from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite_node_repository import SQLiteNodeRepository


def test_save_nodes_uses_node_timestamp():
    # Arrange
    mock_db_manager = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    @contextmanager
    def scope():
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
    repo.save_nodes([node])

    # Assert
    # Check that execute was called with the specific timestamp
    args, _ = mock_cursor.execute.call_args
    query, params = args

    # The first parameter should be the timestamp string
    inserted_ts = params[0]
    assert inserted_ts == specific_ts.isoformat()


def test_save_nodes_uses_current_time_if_none():
    # Arrange
    mock_db_manager = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.__exit__.return_value = None
    mock_conn.cursor.return_value = mock_cursor

    @contextmanager
    def scope():
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
    repo.save_nodes([node])

    # Assert
    args, _ = mock_cursor.execute.call_args
    query, params = args

    # The first parameter should be a timestamp string (current time)
    inserted_ts = params[0]
    assert isinstance(inserted_ts, str)
    # Should be recent
    dt = datetime.fromisoformat(inserted_ts)
    assert dt is not None
