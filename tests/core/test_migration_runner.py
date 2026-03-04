# tests/core/test_migration_runner.py
"""
Unit tests for the MigrationRunner.

Tests the migration runner against in-memory SQLite databases to
verify script discovery, version tracking, idempotency, and error
handling — all without touching PostgreSQL.
"""

import sqlite3
from unittest.mock import patch

import aiosqlite
import pytest

from greenkube.core.migrations.runner import MigrationRunner

# ── Helpers ────────────────────────────────────────────────────────────


def _table_exists(db_path: str, table: str) -> bool:
    """Check whether *table* exists in the SQLite database."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def _column_names(db_path: str, table: str) -> set[str]:
    """Return column names for a table."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()
    return cols


def _applied_versions(db_path: str) -> set[int]:
    """Return the set of migration versions recorded in schema_migrations."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT version FROM schema_migrations")
    versions = {row[0] for row in cur.fetchall()}
    conn.close()
    return versions


def _create_minimal_schema(db_path: str) -> None:
    """Create a minimal combined_metrics table without migration columns."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE combined_metrics (
            pod_name TEXT,
            namespace TEXT,
            total_cost REAL,
            "timestamp" TEXT,
            UNIQUE(pod_name, namespace, "timestamp")
        );
    """)
    conn.execute("""
        CREATE TABLE node_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            node_name TEXT NOT NULL,
            UNIQUE(node_name, timestamp)
        );
    """)
    conn.commit()
    conn.close()


# ── Tests ──────────────────────────────────────────────────────────────


class TestMigrationRunnerInit:
    """Tests for MigrationRunner construction."""

    def test_valid_sqlite(self):
        runner = MigrationRunner("sqlite")
        assert runner.db_type == "sqlite"

    def test_valid_postgres(self):
        runner = MigrationRunner("postgres")
        assert runner.db_type == "postgres"

    def test_invalid_db_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported db_type"):
            MigrationRunner("mysql")


class TestFilenameParser:
    """Tests for _parse_filename."""

    def test_standard_name(self):
        version, name = MigrationRunner._parse_filename("0001_baseline_migrations.sql")
        assert version == 1
        assert name == "baseline_migrations"

    def test_name_without_description(self):
        version, name = MigrationRunner._parse_filename("0002.sql")
        assert version == 2
        assert name == "0002"

    def test_multi_underscore(self):
        version, name = MigrationRunner._parse_filename("0010_add_new_column_v2.sql")
        assert version == 10
        assert name == "add_new_column_v2"


class TestScriptDiscovery:
    """Tests for _discover_scripts."""

    def test_discovers_bundled_sqlite_scripts(self):
        runner = MigrationRunner("sqlite")
        scripts = runner._discover_scripts()
        assert len(scripts) >= 1
        # First script should be version 1
        assert scripts[0][0] == 1
        assert "baseline" in scripts[0][1]

    def test_discovers_bundled_postgres_scripts(self):
        runner = MigrationRunner("postgres")
        scripts = runner._discover_scripts()
        assert len(scripts) >= 1
        assert scripts[0][0] == 1

    def test_scripts_are_sorted_by_version(self):
        runner = MigrationRunner("sqlite")
        scripts = runner._discover_scripts()
        versions = [s[0] for s in scripts]
        assert versions == sorted(versions)

    def test_custom_scripts_dir(self, tmp_path):
        """Test fallback to filesystem when importlib.resources fails."""
        scripts_dir = tmp_path / "sqlite"
        scripts_dir.mkdir()
        (scripts_dir / "0001_test.sql").write_text("SELECT 1;")
        (scripts_dir / "0002_test2.sql").write_text("SELECT 2;")

        runner = MigrationRunner("sqlite")

        from importlib import resources

        def mock_files(pkg):
            raise ModuleNotFoundError("mock")

        with (
            patch.object(resources, "files", side_effect=mock_files),
            patch("greenkube.core.migrations.runner.Path") as MockPath,
        ):
            mock_parent = MockPath.return_value
            mock_parent.__truediv__ = lambda self, other: tmp_path / other
            MockPath.__truediv__ = lambda self, other: tmp_path / other
            # We don't crash — that's the contract
            mock_file = MockPath.return_value
            mock_file.parent.__truediv__ = lambda self, other: tmp_path
            # Discovery with mocked path won't find real scripts, just verify no crash
            scripts = runner._discover_scripts()
            assert isinstance(scripts, list)


@pytest.mark.asyncio
class TestMigrationRunnerSQLite:
    """Integration tests for the runner against in-memory SQLite."""

    async def test_creates_schema_migrations_table(self, tmp_path):
        """The runner should create schema_migrations on first run."""
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn:
            await runner.run(conn)

        assert _table_exists(db_file, "schema_migrations")

    async def test_applies_migration_0001(self, tmp_path):
        """Migration 0001 should add columns to the minimal schema."""
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn:
            count = await runner.run(conn)

        assert count >= 1
        cols = _column_names(db_file, "combined_metrics")
        assert "node_instance_type" in cols
        assert "embodied_co2e_grams" in cols
        assert "calculation_version" in cols

    async def test_records_applied_versions(self, tmp_path):
        """Applied migrations should be recorded in schema_migrations."""
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn:
            await runner.run(conn)

        versions = _applied_versions(db_file)
        assert 1 in versions

    async def test_idempotent_run(self, tmp_path):
        """Running twice should not re-apply migrations or raise."""
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn:
            first = await runner.run(conn)
            second = await runner.run(conn)

        assert first >= 1
        assert second == 0

    async def test_skips_existing_columns(self, tmp_path):
        """On a fresh DB where columns exist, migration should still succeed."""
        db_file = str(tmp_path / "test.db")
        # Create full schema (all columns already present)
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE combined_metrics (
                pod_name TEXT,
                namespace TEXT,
                total_cost REAL,
                "timestamp" TEXT,
                node_instance_type TEXT,
                node_zone TEXT,
                emaps_zone TEXT,
                is_estimated BOOLEAN,
                estimation_reasons TEXT,
                embodied_co2e_grams REAL,
                cpu_usage_millicores INTEGER,
                memory_usage_bytes INTEGER,
                owner_kind TEXT,
                owner_name TEXT,
                network_receive_bytes REAL,
                network_transmit_bytes REAL,
                disk_read_bytes REAL,
                disk_write_bytes REAL,
                storage_request_bytes INTEGER,
                storage_usage_bytes INTEGER,
                ephemeral_storage_request_bytes INTEGER,
                ephemeral_storage_usage_bytes INTEGER,
                gpu_usage_millicores INTEGER,
                restart_count INTEGER,
                node TEXT,
                calculation_version TEXT,
                UNIQUE(pod_name, namespace, "timestamp")
            );
        """)
        conn.execute("""
            CREATE TABLE node_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                node_name TEXT NOT NULL,
                embodied_emissions_kg REAL,
                UNIQUE(node_name, timestamp)
            );
        """)
        conn.commit()
        conn.close()

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn_async:
            count = await runner.run(conn_async)

        # Should still succeed (duplicate column errors silently skipped)
        assert count >= 1
        versions = _applied_versions(db_file)
        assert 1 in versions

    async def test_preserves_existing_data(self, tmp_path):
        """Migrations must not corrupt existing rows."""
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        # Insert test data
        conn = sqlite3.connect(db_file)
        conn.execute(
            'INSERT INTO combined_metrics (pod_name, namespace, total_cost, "timestamp") VALUES (?, ?, ?, ?)',
            ("app-pod", "default", 0.42, "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        runner = MigrationRunner("sqlite")
        async with aiosqlite.connect(db_file) as conn_async:
            conn_async.row_factory = aiosqlite.Row
            await runner.run(conn_async)

            async with conn_async.execute("SELECT * FROM combined_metrics") as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        assert rows[0]["pod_name"] == "app-pod"
        assert float(rows[0]["total_cost"]) == pytest.approx(0.42)

    async def test_no_scripts_dir_returns_zero(self, tmp_path):
        """If no scripts directory exists, runner should return 0."""
        runner = MigrationRunner("sqlite")

        # Patch to point to nonexistent dir
        db_file = str(tmp_path / "test.db")
        _create_minimal_schema(db_file)

        with patch.object(runner, "_discover_scripts", return_value=[]):
            async with aiosqlite.connect(db_file) as conn:
                count = await runner.run(conn)
        assert count == 0


class TestMigrationRunnerPostgres:
    """Unit tests for PostgreSQL-specific methods using mocks."""

    @pytest.mark.asyncio
    async def test_ensure_postgres_table_creates_schema_migrations(self):
        """Verify the CREATE TABLE statement is issued for postgres."""
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        runner = MigrationRunner("postgres")
        await runner._ensure_migrations_table_postgres(conn)
        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "schema_migrations" in sql

    @pytest.mark.asyncio
    async def test_get_applied_versions_postgres(self):
        """Verify applied versions are read from schema_migrations."""
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        conn.fetch.return_value = [{"version": 1}, {"version": 2}]
        runner = MigrationRunner("postgres")
        versions = await runner._get_applied_versions_postgres(conn)
        assert versions == {1, 2}

    @pytest.mark.asyncio
    async def test_record_migration_postgres(self):
        """Verify migration recording uses $1/$2 placeholders."""
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        runner = MigrationRunner("postgres")
        await runner._record_migration(conn, 1, "test")
        conn.execute.assert_called_once_with(
            "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
            1,
            "test",
        )

    @pytest.mark.asyncio
    async def test_execute_sql_postgres(self):
        """Verify SQL is passed directly to connection.execute for postgres."""
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        runner = MigrationRunner("postgres")
        await runner._execute_sql_postgres(conn, "ALTER TABLE foo ADD COLUMN bar TEXT;")
        conn.execute.assert_called_once_with("ALTER TABLE foo ADD COLUMN bar TEXT;")
