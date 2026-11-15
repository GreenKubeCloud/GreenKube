"""Additive measures of the PostgreSQL metric tables are double precision (NRG-06)."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner
from greenkube.core.migrations.online import complete_online_column_upgrades, pending_online_upgrades

METRIC_TABLES = ("combined_metrics", "combined_metrics_hourly", "container_metrics", "container_metrics_hourly")
MEASURES = ("joules", "co2e_grams", "embodied_co2e_grams", "total_cost", "pue", "grid_intensity")
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _dsn() -> str:
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL storage tests.")
    return dsn


async def _schema(connection, prefix: str) -> str:
    schema = f"{prefix}_{uuid4().hex[:12]}"
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    return schema


async def _migrate(connection, *, up_to: int | None = None, after: int = -1) -> None:
    runner = MigrationRunner("postgres")
    await runner._ensure_migrations_table(connection)
    for version, name, sql in runner._discover_scripts():
        if version <= after:
            continue
        if up_to is not None and version > up_to:
            break
        await runner._execute_sql(connection, sql)
        await runner._record_migration(connection, version, name)


async def _measure_types(connection) -> dict[tuple[str, str], str]:
    rows = await connection.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[]) AND column_name = ANY($2::text[])",
        list(METRIC_TABLES),
        list(MEASURES),
    )
    return {(row["table_name"], row["column_name"]): row["data_type"] for row in rows}


async def test_measure_columns_are_double_precision_after_migration():
    connection = await asyncpg.connect(_dsn())
    schema = await _schema(connection, "gk_nrg06")
    try:
        await MigrationRunner("postgres").run(connection)
        types = await _measure_types(connection)
        assert len(types) == len(METRIC_TABLES) * len(MEASURES)
        assert set(types.values()) == {"double precision"}, types
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


async def test_sum_of_many_small_rows_does_not_drift():
    """300k rows of 1e-4: a float4 sum is already 0.3 % short (29.8998), a float8 sum is exact to 1e-6."""
    connection = await asyncpg.connect(_dsn())
    schema = await _schema(connection, "gk_nrg06_sum")
    try:
        await MigrationRunner("postgres").run(connection)
        rows = 300_000
        await connection.execute(
            "INSERT INTO combined_metrics (pod_name, namespace, timestamp, joules, co2e_grams, total_cost) "
            "SELECT 'p' || i, 'ns', $1::timestamptz, 1e-4, 1e-4, 1e-4 FROM generate_series(1, $2) AS i",
            T0,
            rows,
        )
        totals = await connection.fetchrow(
            "SELECT SUM(joules) AS joules, SUM(co2e_grams) AS co2e, SUM(total_cost) AS cost FROM combined_metrics"
        )
        for value in totals.values():
            assert value == pytest.approx(30.0, rel=1e-6)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


async def test_large_tables_are_upgraded_online_in_batches_with_a_bounded_swap_lock():
    dsn = _dsn()
    connection = await asyncpg.connect(dsn)
    schema = await _schema(connection, "gk_nrg06_online")
    reader = await asyncpg.connect(dsn)
    try:
        await _migrate(connection, up_to=144)
        for index in range(5):
            await connection.execute(
                "INSERT INTO combined_metrics (pod_name, namespace, timestamp, joules, total_cost) "
                "VALUES ($1, 'ns', $2, 1.5, 0.25)",
                f"p{index}",
                T0,
            )
            await connection.execute(
                "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket, joules) "
                "VALUES ($1, 'ns', $2, 2.5)",
                f"p{index}",
                T0,
            )
        # Force the online path whatever the table size.
        await connection.execute("SET greenkube.online_upgrade_min_bytes = '0'")
        await _migrate(connection, after=144, up_to=145)
        await connection.execute("RESET greenkube.online_upgrade_min_bytes")
        await _migrate(connection, after=145)

        assert set((await pending_online_upgrades(connection))["combined_metrics"]) == set(MEASURES)
        assert set((await _measure_types(connection)).values()) == {"real"}
        # A row written during the transition reaches the shadow column through the trigger.
        await connection.execute(
            "INSERT INTO combined_metrics (pod_name, namespace, timestamp, joules) VALUES ('late', 'ns', $1, 7.0)",
            T0 + timedelta(minutes=5),
        )
        assert await connection.fetchval("SELECT joules__f8 FROM combined_metrics WHERE pod_name = 'late'") == 7.0

        # A reader holding the table: the batched backfill still runs, the swap gives up within its timeout.
        await reader.execute(f'SET search_path TO "{schema}"')
        transaction = reader.transaction()
        await transaction.start()
        await reader.fetchval("SELECT COUNT(*) FROM combined_metrics")
        completed = await complete_online_column_upgrades(connection, batch_rows=2, swap_lock_timeout_ms=200)
        assert "combined_metrics" not in completed
        assert await connection.fetchval("SELECT COUNT(*) FROM combined_metrics WHERE joules__f8 IS NULL") == 0
        await transaction.rollback()

        completed = await complete_online_column_upgrades(connection, batch_rows=2)
        assert "combined_metrics" in completed
        assert await pending_online_upgrades(connection) == {}
        assert set((await _measure_types(connection)).values()) == {"double precision"}
        assert await connection.fetchval("SELECT SUM(joules) FROM combined_metrics") == pytest.approx(5 * 1.5 + 7.0)
        assert await connection.fetchval("SELECT SUM(total_cost) FROM combined_metrics") == pytest.approx(1.25)
        # The defaults of the hourly table are kept and writers use the same column names.
        await connection.execute(
            "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket) VALUES ('new', 'ns', $1)", T0
        )
        defaults = await connection.fetchrow("SELECT joules, pue FROM combined_metrics_hourly WHERE pod_name = 'new'")
        assert defaults["joules"] == 0 and defaults["pue"] == 1.0
        leftovers = await connection.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND column_name LIKE '%\\_\\_f8'"
        )
        assert leftovers == 0
    finally:
        await reader.close()
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
