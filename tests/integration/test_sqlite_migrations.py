# tests/integration/test_sqlite_migrations.py
"""
Tests for SQLite schema migrations via the versioned MigrationRunner.

Creates a database with a minimal "old" schema (missing columns that
should be added by migrations) and verifies that ``setup_sqlite`` brings
it up to date without losing existing data.

See: ARCH-001 in the Show HN plan.
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


def _table_exists(path: str, table: str) -> bool:
    """Check whether a table exists in the SQLite database."""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    exists = cur.fetchone() is not None
    conn.close()
    return exists


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_adds_missing_columns(tmp_path):
    """setup_sqlite should run versioned migrations to add columns missing from v1."""
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
    # Second run — migrations should be detected as already applied
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    after = _column_names(db_file, "combined_metrics")
    assert "embodied_co2e_grams" in after


@pytest.mark.asyncio
async def test_schema_migrations_table_created(tmp_path):
    """setup_sqlite should create the schema_migrations tracking table."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    assert _table_exists(db_file, "schema_migrations")


@pytest.mark.asyncio
async def test_migration_versions_are_recorded(tmp_path):
    """Applied migration versions should be recorded in schema_migrations."""
    db_file = str(tmp_path / "v1.db")
    _create_v1_schema(db_file)

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    conn = sqlite3.connect(db_file)
    cur = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) >= 1
    assert rows[0][0] == 1
    assert "baseline" in rows[0][1]


@pytest.mark.asyncio
async def test_fresh_db_records_no_duplicate_errors(tmp_path):
    """A fresh DB (full CREATE TABLE schema) should not fail on migration."""
    db_file = str(tmp_path / "fresh.db")

    mgr = DatabaseManager()
    await mgr.setup_sqlite(db_path=db_file)
    await mgr.close()

    # All tables should exist and schema_migrations should track version 1
    assert _table_exists(db_file, "schema_migrations")
    assert _table_exists(db_file, "combined_metrics")

    conn = sqlite3.connect(db_file)
    cur = conn.execute("SELECT version FROM schema_migrations")
    versions = {row[0] for row in cur.fetchall()}
    conn.close()

    assert 1 in versions
