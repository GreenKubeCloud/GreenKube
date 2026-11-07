"""Upgrade tests for the savings ledger migrations 0042 and 0043 (roadmap C0.4, C0.6, C0.7)."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository

LAST_VERSION_BEFORE_LEDGER_V2 = 41


async def _sqlite_at(path: str, target_version: int) -> None:
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(path) as connection:
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version > target_version:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.commit()


async def _postgres_at(connection, target_version: int) -> None:
    runner = MigrationRunner("postgres")
    await runner._ensure_migrations_table(connection)
    for version, name, sql in runner._discover_scripts():
        if version > target_version:
            break
        await runner._execute_sql(connection, sql)
        await runner._record_migration(connection, version, name)


@pytest.mark.asyncio
async def test_sqlite_upgrade_flags_legacy_rows_and_keeps_totals(tmp_path, monkeypatch):
    db_file = str(tmp_path / "ledger-upgrade.db")
    await _sqlite_at(db_file, LAST_VERSION_BEFORE_LEDGER_V2)
    with sqlite3.connect(db_file) as connection:
        # Raw 5-minute rows whose period_start was back-filled by migration 0018 with datetime().
        connection.executemany(
            """
            INSERT INTO recommendation_savings_ledger
                (recommendation_id, cluster_name, namespace, recommendation_type, co2e_saved_grams,
                 cost_saved_dollars, period_seconds, timestamp, period_start, period_end, measurement_method)
            VALUES (?, 'c', 'prod', 'RIGHTSIZING_CPU', ?, ?, 300, ?, ?, ?, ?)
            """,
            [
                (1, 1.0, 0.1, "2026-10-02T10:35:00Z", "2026-10-02 10:30:00", "2026-10-02T10:35:00Z", "prorated"),
                (1, 1.0, 0.1, "2026-10-02T10:40:00Z", "2026-10-02 10:35:00", "2026-10-02T10:40:00Z", "prorated"),
                (2, 3.0, 0.3, "2026-10-02T10:40:00Z", "2026-10-02T10:35:00Z", "2026-10-02T10:40:00Z", "measured"),
            ],
        )
        # Rows left by the removed hourly compression job.
        connection.executemany(
            """
            INSERT INTO recommendation_savings_ledger_hourly
                (recommendation_id, cluster_name, namespace, recommendation_type, co2e_saved_grams,
                 cost_saved_dollars, sample_count, hour_bucket, measurement_method, period_start, period_end)
            VALUES (?, 'c', 'prod', 'RIGHTSIZING_CPU', ?, ?, ?, ?, 'prorated', ?, ?)
            """,
            [
                (1, 4.0, 0.4, 4, "2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z", "2026-10-01 10:00:00"),
                (1, 12.0, 1.2, 12, "2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"),
            ],
        )

    monkeypatch.setenv("DB_PATH", db_file)
    from greenkube.core.config import config

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repo = SQLiteSavingsLedgerRepository(manager)
        totals = await repo.get_cumulative_totals("c", group_by_method=True)
        assert totals["projected"]["co2e_saved_grams"] == pytest.approx(18.0)
        assert totals["measured"]["co2e_saved_grams"] == pytest.approx(3.0)
        assert await repo.has_legacy_compressed_rows("c")
        start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
        assert await repo.has_legacy_compressed_rows("c", start, start + timedelta(hours=1))
        assert not await repo.has_legacy_compressed_rows("c", start + timedelta(days=1), start + timedelta(days=2))
        # The diagnostic follows the namespace filter of the report.
        assert await repo.has_legacy_compressed_rows("c", start, start + timedelta(hours=1), namespace="prod")
        assert not await repo.has_legacy_compressed_rows("c", start, start + timedelta(hours=1), namespace="dev")
        assert not await repo.has_legacy_compressed_rows("c", namespace="")
        # The partial first legacy hour is included pro rata (BUG-40).
        window = await repo.get_window_totals("c", start, start + timedelta(hours=1, minutes=30))
        assert window["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(2.0 + 12.0)
        floors = await repo.get_accrual_floors([1, 2])
        assert floors[1] == datetime(2026, 10, 2, 10, 40, tzinfo=timezone.utc)
    finally:
        await manager.close()

    with sqlite3.connect(db_file) as connection:
        assert connection.execute(
            "SELECT DISTINCT measurement_method FROM recommendation_savings_ledger ORDER BY 1"
        ).fetchall() == [("measured",), ("projected",)]
        assert connection.execute(
            "SELECT legacy_compressed, period_end FROM recommendation_savings_ledger_hourly ORDER BY hour_bucket"
        ).fetchall() == [(1, "2026-10-01T10:00:00Z"), (1, "2026-10-01T11:00:00Z")]
        assert connection.execute(
            "SELECT period_start FROM recommendation_savings_ledger ORDER BY period_start"
        ).fetchall() == [("2026-10-02T10:30:00Z",), ("2026-10-02T10:35:00Z",), ("2026-10-02T10:35:00Z",)]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert {"accrual_watermarks", "savings_accruals_v1", "savings_accruals_v1_hourly_rollup"} <= tables
        # savings_ledger_entries is reserved for the roadmap C6 ledger table.
        assert "savings_ledger_entries" not in tables


@pytest.mark.asyncio
async def test_postgres_upgrade_flags_legacy_rows_and_keeps_totals(monkeypatch):
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL savings ledger migration tests.")
    schema = f"gk_ledger_upgrade_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await _postgres_at(connection, LAST_VERSION_BEFORE_LEDGER_V2)
        await connection.executemany(
            """
            INSERT INTO recommendation_savings_ledger
                (recommendation_id, cluster_name, namespace, recommendation_type, co2e_saved_grams,
                 cost_saved_dollars, period_seconds, timestamp, period_start, period_end, measurement_method)
            VALUES ($1, 'c', 'prod', 'RIGHTSIZING_CPU', $2, $3, 300, $4::TIMESTAMPTZ,
                    $4::TIMESTAMPTZ - INTERVAL '5 minutes', $4::TIMESTAMPTZ, $5)
            """,
            [
                (1, 1.0, 0.1, datetime(2026, 10, 2, 10, 35, tzinfo=timezone.utc), "prorated"),
                (1, 1.0, 0.1, datetime(2026, 10, 2, 10, 40, tzinfo=timezone.utc), "prorated"),
                (2, 3.0, 0.3, datetime(2026, 10, 2, 10, 40, tzinfo=timezone.utc), "measured"),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO recommendation_savings_ledger_hourly
                (recommendation_id, cluster_name, namespace, recommendation_type, co2e_saved_grams,
                 cost_saved_dollars, sample_count, hour_bucket, measurement_method, period_start, period_end)
            VALUES ($1, 'c', 'prod', 'RIGHTSIZING_CPU', $2, $3, $4, $5::TIMESTAMPTZ, 'prorated',
                    $5::TIMESTAMPTZ, $5::TIMESTAMPTZ + INTERVAL '1 hour')
            """,
            [
                (1, 4.0, 0.4, 4, datetime(2026, 10, 1, 9, tzinfo=timezone.utc)),
                (1, 12.0, 1.2, 12, datetime(2026, 10, 1, 10, tzinfo=timezone.utc)),
            ],
        )
    finally:
        await connection.close()

    monkeypatch.setenv("DB_TYPE", "postgres")
    monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
    monkeypatch.setenv("DB_SCHEMA", schema)
    monkeypatch.setenv("DB_SSL_MODE", "disable")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    from greenkube.core.config import config

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repo = PostgresSavingsLedgerRepository(manager)
        totals = await repo.get_cumulative_totals("c", group_by_method=True)
        assert totals["projected"]["co2e_saved_grams"] == pytest.approx(18.0)
        assert totals["measured"]["co2e_saved_grams"] == pytest.approx(3.0)
        assert await repo.has_legacy_compressed_rows("c")
        start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
        assert await repo.has_legacy_compressed_rows("c", start, start + timedelta(hours=1), namespace="prod")
        assert not await repo.has_legacy_compressed_rows("c", start, start + timedelta(hours=1), namespace="dev")
        assert not await repo.has_legacy_compressed_rows("c", namespace="")
        window = await repo.get_window_totals("c", start, start + timedelta(hours=1, minutes=30))
        assert window["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(2.0 + 12.0)
        async with manager.connection_scope() as conn:
            methods = await conn.fetch(  # type: ignore[union-attr]
                "SELECT DISTINCT measurement_method FROM recommendation_savings_ledger"
            )
            flags = await conn.fetch(  # type: ignore[union-attr]
                "SELECT DISTINCT legacy_compressed FROM recommendation_savings_ledger_hourly"
            )
            column_type = await conn.fetchval(  # type: ignore[union-attr]
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = 'recommendation_savings_ledger'
                  AND column_name = 'co2e_saved_grams'
                """,
                schema,
            )
            views = await conn.fetch(  # type: ignore[union-attr]
                "SELECT table_name FROM information_schema.views WHERE table_schema = $1",
                schema,
            )
        assert {"savings_accruals_v1", "savings_accruals_v1_hourly_rollup"} <= {row[0] for row in views}
        # savings_ledger_entries is reserved for the roadmap C6 ledger table.
        assert "savings_ledger_entries" not in {row[0] for row in views}
        assert {row[0] for row in methods} == {"projected", "measured"}
        assert [row[0] for row in flags] == [True]
        assert column_type == "double precision"
    finally:
        await manager.close()
        cleanup = await asyncpg.connect(dsn)
        try:
            await cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await cleanup.close()
