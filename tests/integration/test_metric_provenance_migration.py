"""Upgrade tests for the metric provenance (0039) and compression watermark (0040) migrations.

A database migrated up to the schema that precedes them is filled with legacy
pod, container and hourly rows, then upgraded. Legacy rows must be backfilled
as roadmap A11 specifies: ``cost_source = opencost`` when a real cost was
stored (``none`` otherwise, including rows priced by the missing-cost
default), ``usage_source = prometheus`` when usage was stored,
``intensity_source = unknown``; container rows inherit their pod's
``cost_source`` (their cost was allocated from it) and ``emaps_zone`` (so the
intensity recompute, A9, can reach them). Zone defaults that older collectors
persisted into ``carbon_intensity_history`` (``estimation_method =
default_fallback``) are purged so they never pass for measurements (A8, BUG-07).
"""

from __future__ import annotations

import os
import sqlite3
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

#: Last schema version before this work package's migrations.
PREVIOUS_VERSION = 38

NO_COST_REASON = "[\"No cost data for pod 'cache-0'\"]"

POD_ROWS = [
    # pod_name, total_cost, cpu_usage_millicores, estimation_reasons, emaps_zone
    ("api-0", 0.25, 120, "[]", "FR"),
    ("cache-0", 0.10, None, NO_COST_REASON, "DE"),
    ("idle-0", 0.0, 5, "[]", "FR"),
]
CONTAINER_ROWS = [
    # pod_name, container_name, total_cost, cpu_usage_millicores
    ("api-0", "app", 0.2, 100),
    ("api-0", "sidecar", 0.05, None),
    ("cache-0", "redis", 0.10, None),
    ("orphan-0", "app", 0.30, 50),
]
HOURLY_ROWS = [
    # pod_name, total_cost, cpu_usage_avg
    ("api-0", 3.0, 110),
    ("idle-0", 0.0, None),
]

EXPECTED_PODS = {
    "api-0": ("opencost", "prometheus", "unknown"),
    "cache-0": ("none", None, "unknown"),
    "idle-0": ("none", "prometheus", "unknown"),
}
EXPECTED_CONTAINERS = {
    ("api-0", "app"): ("opencost", "prometheus", "unknown", "FR"),
    ("api-0", "sidecar"): ("opencost", None, "unknown", "FR"),
    # Inherits the pod's missing-cost "none" although its own reasons are empty.
    ("cache-0", "redis"): ("none", None, "unknown", "DE"),
    # No pod row: the rule applies to the container row itself.
    ("orphan-0", "app"): ("opencost", "prometheus", "unknown", None),
}
EXPECTED_HOURLY = {
    "api-0": ("opencost", "prometheus", "unknown"),
    "idle-0": ("none", None, "unknown"),
}
NEW_COLUMNS = {
    "cost_cpu",
    "cost_ram",
    "cost_storage",
    "cost_network",
    "cost_lb",
    "cost_source",
    "usage_source",
    "power_source",
    "intensity_source",
}
CONTAINER_ONLY_COLUMNS = {"emaps_zone", "cpu_limit", "memory_limit"}
INTENSITY_ROWS = [
    # zone, carbon_intensity, datetime, is_estimated, estimation_method
    ("FR", 21.0, "2026-10-01T09:00:00Z", True, "default_fallback"),
    ("FR", 48.0, "2026-10-01T08:00:00Z", False, None),
    ("DE", 310.0, "2026-10-01T09:00:00Z", True, "TIME_SLICER_AVERAGE"),
]
#: Measured points survive the upgrade; persisted defaults are gone.
EXPECTED_INTENSITY = {("FR", 48.0), ("DE", 310.0)}


async def _sqlite_at_previous_version(path: str) -> None:
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(path) as connection:
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version > PREVIOUS_VERSION:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.commit()


async def test_sqlite_upgrade_backfills_provenance_of_legacy_rows(tmp_path):
    db_file = str(tmp_path / "legacy-metrics.db")
    await _sqlite_at_previous_version(db_file)
    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            'INSERT INTO combined_metrics (pod_name, namespace, "timestamp", total_cost, cpu_usage_millicores, '
            "estimation_reasons, emaps_zone) VALUES (?, 'shop', '2026-10-01T10:00:00Z', ?, ?, ?, ?)",
            POD_ROWS,
        )
        connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, total_cost, "
            "cpu_usage_millicores) VALUES (?, 'shop', ?, '2026-10-01T10:00:00Z', ?, ?)",
            CONTAINER_ROWS,
        )
        connection.executemany(
            "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket, total_cost, cpu_usage_avg) "
            "VALUES (?, 'shop', '2026-09-30T10:00:00Z', ?, ?)",
            HOURLY_ROWS,
        )
        connection.executemany(
            "INSERT INTO carbon_intensity_history (zone, carbon_intensity, datetime, is_estimated, "
            "estimation_method) VALUES (?, ?, ?, ?, ?)",
            INTENSITY_ROWS,
        )

    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(db_file) as connection:
        assert await runner.run(connection) >= 2
        assert await runner.run(connection) == 0

    with sqlite3.connect(db_file) as connection:
        for table in ("combined_metrics", "combined_metrics_hourly", "container_metrics", "container_metrics_hourly"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert NEW_COLUMNS <= columns, table
            if table.startswith("container"):
                assert CONTAINER_ONLY_COLUMNS <= columns, table
        pods = {
            row[0]: tuple(row[1:])
            for row in connection.execute(
                "SELECT pod_name, cost_source, usage_source, intensity_source FROM combined_metrics"
            )
        }
        containers = {
            (row[0], row[1]): tuple(row[2:])
            for row in connection.execute(
                "SELECT pod_name, container_name, cost_source, usage_source, intensity_source, emaps_zone "
                "FROM container_metrics"
            )
        }
        hourly = {
            row[0]: tuple(row[1:])
            for row in connection.execute(
                "SELECT pod_name, cost_source, usage_source, intensity_source FROM combined_metrics_hourly"
            )
        }
        watermarks = connection.execute("SELECT COUNT(*) FROM metrics_compression_watermarks").fetchone()
        intensities = set(connection.execute("SELECT zone, carbon_intensity FROM carbon_intensity_history"))

    assert pods == EXPECTED_PODS
    assert containers == EXPECTED_CONTAINERS
    assert hourly == EXPECTED_HOURLY
    assert intensities == EXPECTED_INTENSITY
    # The watermark starts empty: the first compression run derives it from the raw tables.
    assert watermarks == (0,)


async def test_postgres_upgrade_backfills_provenance_of_legacy_rows():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration upgrade tests.")

    schema = f"gk_provenance_upgrade_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        runner = MigrationRunner("postgres")
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version > PREVIOUS_VERSION:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)

        await connection.executemany(
            "INSERT INTO combined_metrics (pod_name, namespace, timestamp, total_cost, cpu_usage_millicores, "
            "estimation_reasons, emaps_zone) VALUES ($1, 'shop', '2026-10-01T10:00:00Z', $2, $3, $4, $5)",
            POD_ROWS,
        )
        await connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, total_cost, "
            "cpu_usage_millicores) VALUES ($1, 'shop', $2, '2026-10-01T10:00:00Z', $3, $4)",
            CONTAINER_ROWS,
        )
        await connection.executemany(
            "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket, total_cost, cpu_usage_avg) "
            "VALUES ($1, 'shop', '2026-09-30T10:00:00Z', $2, $3)",
            HOURLY_ROWS,
        )
        await connection.executemany(
            "INSERT INTO carbon_intensity_history (zone, carbon_intensity, datetime, is_estimated, "
            "estimation_method) VALUES ($1, $2, $3::text::timestamptz, $4, $5)",
            INTENSITY_ROWS,
        )

        assert await MigrationRunner("postgres").run(connection) >= 2
        assert await MigrationRunner("postgres").run(connection) == 0

        for table in ("combined_metrics", "combined_metrics_hourly", "container_metrics", "container_metrics_hourly"):
            columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
                    schema,
                    table,
                )
            }
            assert NEW_COLUMNS <= columns, table
            if table.startswith("container"):
                assert CONTAINER_ONLY_COLUMNS <= columns, table
        pods = {
            row["pod_name"]: (row["cost_source"], row["usage_source"], row["intensity_source"])
            for row in await connection.fetch(
                "SELECT pod_name, cost_source, usage_source, intensity_source FROM combined_metrics"
            )
        }
        containers = {
            (row["pod_name"], row["container_name"]): (
                row["cost_source"],
                row["usage_source"],
                row["intensity_source"],
                row["emaps_zone"],
            )
            for row in await connection.fetch(
                "SELECT pod_name, container_name, cost_source, usage_source, intensity_source, emaps_zone "
                "FROM container_metrics"
            )
        }
        hourly = {
            row["pod_name"]: (row["cost_source"], row["usage_source"], row["intensity_source"])
            for row in await connection.fetch(
                "SELECT pod_name, cost_source, usage_source, intensity_source FROM combined_metrics_hourly"
            )
        }
        watermarks = await connection.fetchval(
            "SELECT COUNT(*) FROM metrics_compression_watermarks WHERE compressed_until IS NOT NULL"
        )
        intensities = {
            (row["zone"], row["carbon_intensity"])
            for row in await connection.fetch("SELECT zone, carbon_intensity FROM carbon_intensity_history")
        }
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()

    assert pods == EXPECTED_PODS
    assert containers == EXPECTED_CONTAINERS
    assert hourly == EXPECTED_HOURLY
    assert intensities == EXPECTED_INTENSITY
    assert watermarks == 0
