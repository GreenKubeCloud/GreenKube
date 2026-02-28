# tests/integration/test_sqlite_migrations.py
"""
Tests for SQLite schema migrations.

Creates a database with a minimal "old" schema (missing columns that
should be added by migrations) and verifies that ``setup_sqlite`` brings
it up to date without losing existing data.

See: TEST-002 in the issue plan.
"""

import sqlite3

import aiosqlite
import pytest

from greenkube.core.db import DatabaseManager


def _create_v1_schema(path: str) -> None:
    """Create a minimal v1 schema missing all migration columns."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE combined_metrics (
            pod_name TEXT,
            namespace TEXT,
            total_cost REAL,
            co2e_grams REAL,
            pue REAL,
            grid_intensity REAL,
            joules REAL,
            watts REAL,
            cpu_request INTEGER,
            cpu_limit INTEGER,
            memory_request INTEGER,
            memory_limit INTEGER,
            cpu_usage_avg REAL,
            memory_usage_avg REAL,
            period TEXT,
            "timestamp" TEXT,
            duration_seconds INTEGER,
            grid_intensity_timestamp TEXT,
            UNIQUE(pod_name, namespace, "timestamp")
        );
    """)
    conn.execute("""
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
    # Insert a row so we can verify data survives migration
    conn.execute(
        'INSERT INTO combined_metrics (pod_name, namespace, total_cost, co2e_grams, joules, "timestamp") '
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("test-pod", "test-ns", 0.01, 1.5, 100.0, "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def _column_names(path: str, table: str) -> set[str]:
    """Return column names for a table."""
    conn = sqlite3.connect(path)
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()
    return cols


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_adds_missing_columns(tmp_path):
    """setup_sqlite should ALTER TABLE to add columns missing from v1."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    before = _column_names(db_file, "combined_metrics")
    assert "node_instance_type" not in before
    assert "embodied_co2e_grams" not in before

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    after = _column_names(db_file, "combined_metrics")
    expected_new = {
        "node_instance_type",
        "node_zone",
        "emaps_zone",
        "is_estimated",
        "estimation_reasons",
        "embodied_co2e_grams",
        "cpu_usage_millicores",
        "memory_usage_bytes",
        "owner_kind",
        "owner_name",
        "node",
        "calculation_version",
        "network_receive_bytes",
        "network_transmit_bytes",
        "disk_read_bytes",
        "disk_write_bytes",
        "storage_request_bytes",
        "storage_usage_bytes",
        "ephemeral_storage_request_bytes",
        "ephemeral_storage_usage_bytes",
        "gpu_usage_millicores",
        "restart_count",
    }
    for col in expected_new:
        assert col in after, f"Migration did not add column: {col}"


@pytest.mark.asyncio
async def test_migration_adds_node_snapshots_column(tmp_path):
    """Migration should add embodied_emissions_kg to node_snapshots."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    before = _column_names(db_file, "node_snapshots")
    assert "embodied_emissions_kg" not in before

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    after = _column_names(db_file, "node_snapshots")
    assert "embodied_emissions_kg" in after


@pytest.mark.asyncio
async def test_migration_preserves_existing_data(tmp_path):
    """Existing rows should survive the migration without corruption."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)

    async with aiosqlite.connect(db_file) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM combined_metrics") as cur:
            rows = await cur.fetchall()

    await mgr.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["pod_name"] == "test-pod"
    assert row["namespace"] == "test-ns"
    assert float(row["co2e_grams"]) == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Running setup_sqlite twice should not raise errors."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    # Second run — all ALTER TABLEs should be no-ops
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    after = _column_names(db_file, "combined_metrics")
    assert "embodied_co2e_grams" in after
