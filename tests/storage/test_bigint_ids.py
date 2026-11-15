"""Ids and sequences of the PostgreSQL schema are 64-bit (STO-05)."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.db import warn_on_exhausting_sequences
from greenkube.core.migrations import MigrationRunner
from greenkube.core.migrations.online import complete_online_column_upgrades, pending_online_upgrades

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
INSERT = "INSERT INTO combined_metrics (pod_name, namespace, timestamp, joules) VALUES ($1, 'ns', $2, 1.0)"


def _dsn() -> str:
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL id tests.")
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


async def _int4_ids(connection) -> list[str]:
    columns = await connection.fetch(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND column_name = 'id' AND data_type = 'integer' ORDER BY 1"
    )
    sequences = await connection.fetch(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = current_schema() AND data_type = 'integer'::regtype"
    )
    return [row[0] for row in columns] + [row[0] for row in sequences]


async def test_every_id_is_bigint_and_writes_continue_past_the_32_bit_limit():
    connection = await asyncpg.connect(_dsn())
    schema = await _schema(connection, "gk_sto05")
    try:
        await MigrationRunner("postgres").run(connection)
        assert await _int4_ids(connection) == []
        await connection.execute("SELECT setval(pg_get_serial_sequence('combined_metrics', 'id'), 2147483640)")
        for index in range(20):
            await connection.execute(INSERT, f"p{index}", T0)
        assert await connection.fetchval("SELECT MAX(id) FROM combined_metrics") == 2147483660
        assert await warn_on_exhausting_sequences(connection) == []
        await connection.execute("SELECT setval(pg_get_serial_sequence('combined_metrics', 'id'), 5000000000000000000)")
        assert await warn_on_exhausting_sequences(connection) == ["combined_metrics_id_seq"]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


async def test_large_metric_tables_get_their_bigint_id_online():
    connection = await asyncpg.connect(_dsn())
    schema = await _schema(connection, "gk_sto05_online")
    try:
        await _migrate(connection, up_to=146)
        for index in range(5):
            await connection.execute(INSERT, f"p{index}", T0)
        await connection.execute("SET greenkube.online_upgrade_min_bytes = '0'")
        await _migrate(connection, after=146)
        await connection.execute("RESET greenkube.online_upgrade_min_bytes")

        assert (await pending_online_upgrades(connection))["combined_metrics"] == ["id"]
        # Small tables are converted in place, the sequence of the large one at once.
        assert await _int4_ids(connection) == ["combined_metrics", "combined_metrics_hourly"]
        await connection.execute(INSERT, "late", T0 + timedelta(minutes=5))

        completed = await complete_online_column_upgrades(connection, batch_rows=2)
        assert set(completed) == {"combined_metrics", "combined_metrics_hourly"}
        assert await _int4_ids(connection) == []
        assert await pending_online_upgrades(connection) == {}
        ids = [row[0] for row in await connection.fetch("SELECT id FROM combined_metrics ORDER BY id")]
        assert ids == [1, 2, 3, 4, 5, 6]
        primary_key = await connection.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'combined_metrics'::regclass AND contype = 'p'"
        )
        assert primary_key == "PRIMARY KEY (id)"
        await connection.execute("SELECT setval(pg_get_serial_sequence('combined_metrics', 'id'), 2147483647)")
        await connection.execute(INSERT, "after", T0 + timedelta(minutes=10))
        assert await connection.fetchval("SELECT id FROM combined_metrics WHERE pod_name = 'after'") == 2147483648
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
