"""Migration 0170 queues compressed hours with complete raw rows for one rebuild, not the pruned first hour (review).

The first raw hour of an existing install may have been pruned at a non-aligned
cutoff by the legacy compressor: it is below ``raw_pruned_until`` and must not
be queued (the rebuild would log it as receiving rows after pruning).
"""

from __future__ import annotations

import os
from datetime import datetime
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

ROWS = ["2026-10-01T00:40:00Z"] + [f"2026-10-01T{hour:02d}:10:00Z" for hour in range(1, 7)]
EXPECTED = [f"2026-10-01T{hour:02d}:00:00Z" for hour in range(1, 5)]


async def test_the_sqlite_repair_skips_the_pruned_first_hour(tmp_path):
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(str(tmp_path / "repair.sqlite")) as connection:
        await runner._ensure_migrations_table(connection)
        scripts = runner._discover_scripts()
        for version, name, sql in scripts:
            if version >= 170:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.execute(
            "INSERT INTO metrics_compression_watermarks (table_name, compressed_until, updated_at) "
            "VALUES ('combined_metrics', '2026-10-01T05:00:00Z', '2026-10-02T05:00:00Z')"
        )
        for ts in ROWS:
            await connection.execute(
                "INSERT INTO combined_metrics (pod_name, namespace, \"timestamp\") VALUES ('p', 'ns', ?)", (ts,)
            )
        for version, name, sql in scripts:
            if 170 <= version <= 172:
                await runner._execute_sql(connection, sql)
        async with connection.execute(
            "SELECT hour_bucket FROM metrics_dirty_hours WHERE table_name = 'combined_metrics' ORDER BY hour_bucket"
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == EXPECTED


async def test_the_postgres_repair_skips_the_pruned_first_hour():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL migration test.")
    schema = f"gk_0170_{uuid4().hex}"
    runner = MigrationRunner("postgres")
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await runner._ensure_migrations_table(connection)
        scripts = runner._discover_scripts()
        for version, name, sql in scripts:
            if version >= 170:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.execute(
            "INSERT INTO metrics_compression_watermarks (table_name, compressed_until, updated_at) "
            "VALUES ('combined_metrics', '2026-10-01T05:00:00Z'::timestamptz, now())"
        )
        for ts in ROWS:
            await connection.execute(
                "INSERT INTO combined_metrics (pod_name, namespace, timestamp) VALUES ('p', 'ns', $1)",
                datetime.fromisoformat(ts.replace("Z", "+00:00")),
            )
        for version, name, sql in scripts:
            if 170 <= version <= 172:
                await runner._execute_sql(connection, sql)
        rows = await connection.fetch(
            "SELECT to_char(hour_bucket AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM metrics_dirty_hours "
            "WHERE table_name = 'combined_metrics' ORDER BY hour_bucket"
        )
        assert [row[0] for row in rows] == EXPECTED
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
