# tests/storage/test_node_repository.py

import sqlite3
from datetime import datetime, timezone

import pytest

from greenkube.models.node import NodeInfo
from greenkube.storage.node_repository import NodeRepository

# --- Fixtures ---


@pytest.fixture
def db_connection():
    """Creates an in-memory SQLite database connection for testing."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    # Create the table schema needed for the repository
    cursor.execute("""
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
            UNIQUE(node_name, timestamp)
        );
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def node_repo(db_connection):
    """Creates an instance of the NodeRepository."""
    return NodeRepository(db_connection)


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


def test_save_nodes_new_snapshots(node_repo, db_connection):
    """Test saving multiple new node snapshots successfully."""
    # Act
    saved_count = node_repo.save_nodes(SAMPLE_NODES)

    # Assert
    assert saved_count == 2

    # Verify data in DB
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM node_snapshots")
    assert cursor.fetchone()[0] == 2

    cursor.execute("SELECT instance_type, cpu_capacity_cores FROM node_snapshots WHERE node_name=?", ("node-1",))
    row = cursor.fetchone()
    assert row[0] == "m5.large"
    assert row[1] == 2.0


def test_save_nodes_multiple_snapshots(node_repo, db_connection):
    """Test saving multiple snapshots over time."""
    # Arrange & Act

    # Let's patch datetime in the module
    from unittest.mock import patch

    with patch("greenkube.storage.node_repository.datetime") as mock_datetime:
        # First save
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        node_repo.save_nodes(SAMPLE_NODES)

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
        saved_count = node_repo.save_nodes([updated_node])

    # Assert
    assert saved_count == 1

    # Verify total snapshots
    cursor = db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM node_snapshots WHERE node_name=?", ("node-1",))
    assert cursor.fetchone()[0] == 2

    # Verify latest snapshot
    cursor.execute(
        "SELECT instance_type FROM node_snapshots WHERE node_name=? ORDER BY timestamp DESC LIMIT 1", ("node-1",)
    )
    assert cursor.fetchone()[0] == "m5.xlarge"


def test_save_nodes_empty_list(node_repo):
    """Test saving an empty list of nodes."""
    saved_count = node_repo.save_nodes([])
    assert saved_count == 0


def test_save_nodes_no_connection():
    """Test behavior when connection is None."""
    repo = NodeRepository(None)
    saved_count = repo.save_nodes(SAMPLE_NODES)
    assert saved_count == 0
