"""Repair of stored pod metadata gaps by migration 0270 (COL-01 step 6, roadmap D.2).

Rows written from the Kubernetes state at collection time (``metadata_source``
NULL) or without any pod metadata (``missing``) can carry owner NULL and
requests 0 while other rows of the same pod know them. The migration copies
the owner and the last known requests from those rows, marks the repaired rows
``metadata_source = repaired`` and their ``model_version`` as outdated, so
``greenkube recompute`` re-runs their allocation. Rows of pods that never had
metadata, and rows already carrying metadata, are left alone.
"""

from __future__ import annotations

import os
import sqlite3
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

PREVIOUS_VERSION = 269
MODEL = "impact-1"

POD_ROWS = [
    # pod_name, timestamp, owner_kind, owner_name, cpu_request, memory_request, metadata_source
    ("web-1", "2026-10-01T10:00:00+00:00", "Deployment", "web", 500, 256, "k8s_snapshot"),
    ("web-1", "2026-10-01T10:05:00+00:00", "Deployment", "web", 300, 128, "k8s_snapshot"),
    ("web-1", "2026-10-01T10:10:00+00:00", None, None, 0, 0, "missing"),
    ("web-1", "2026-10-01T10:15:00+00:00", None, None, 0, 0, None),
    ("early-1", "2026-10-01T09:55:00+00:00", None, None, 0, 0, "missing"),
    ("early-1", "2026-10-01T10:00:00+00:00", "Job", "early", 100, 64, "kube_state_metrics"),
    ("bare-1", "2026-10-01T10:00:00+00:00", None, None, 0, 0, "missing"),
]
CONTAINER_ROWS = [
    # pod_name, container_name, timestamp, owner_kind, owner_name
    ("web-1", "app", "2026-10-01T10:15:00+00:00", None, None),
]

EXPECTED_PODS = {
    ("web-1", "10:00"): ("Deployment", "web", 500, 256, "k8s_snapshot", MODEL),
    ("web-1", "10:05"): ("Deployment", "web", 300, 128, "k8s_snapshot", MODEL),
    # Last known requests (10:05), not the first ones.
    ("web-1", "10:10"): ("Deployment", "web", 300, 128, "repaired", MODEL + "+metadata-repaired"),
    ("web-1", "10:15"): ("Deployment", "web", 300, 128, "repaired", MODEL + "+metadata-repaired"),
    # No earlier row: the nearest later one.
    ("early-1", "09:55"): ("Job", "early", 100, 64, "repaired", MODEL + "+metadata-repaired"),
    ("early-1", "10:00"): ("Job", "early", 100, 64, "kube_state_metrics", MODEL),
    # Nothing to repair from: unchanged.
    ("bare-1", "10:00"): (None, None, 0, 0, "missing", MODEL),
}


def _hhmm(value) -> str:
    text = value if isinstance(value, str) else value.isoformat()
    return text[11:16]


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


async def test_sqlite_migration_repairs_pod_metadata_gaps(tmp_path):
    db_file = str(tmp_path / "gaps.db")
    await _sqlite_at_previous_version(db_file)
    with sqlite3.connect(db_file) as connection:
        connection.executemany(
            'INSERT INTO combined_metrics (pod_name, namespace, "timestamp", owner_kind, owner_name, cpu_request, '
            "memory_request, metadata_source, model_version) VALUES (?, 'shop', ?, ?, ?, ?, ?, ?, '" + MODEL + "')",
            POD_ROWS,
        )
        connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, owner_kind, owner_name) "
            "VALUES (?, 'shop', ?, ?, ?, ?)",
            CONTAINER_ROWS,
        )

    async with aiosqlite.connect(db_file) as connection:
        assert await MigrationRunner("sqlite").run(connection) >= 1

    with sqlite3.connect(db_file) as connection:
        pods = {
            (row[0], _hhmm(row[1])): tuple(row[2:])
            for row in connection.execute(
                'SELECT pod_name, "timestamp", owner_kind, owner_name, cpu_request, memory_request, '
                "metadata_source, model_version FROM combined_metrics"
            )
        }
        containers = list(connection.execute("SELECT owner_kind, owner_name FROM container_metrics"))
    assert pods == EXPECTED_PODS
    assert containers == [("Deployment", "web")]


async def test_postgres_migration_repairs_pod_metadata_gaps():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration upgrade tests.")
    from datetime import datetime

    schema = f"gk_pod_repair_{uuid4().hex}"
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
            "INSERT INTO combined_metrics (pod_name, namespace, timestamp, owner_kind, owner_name, cpu_request, "
            "memory_request, metadata_source, model_version) VALUES ($1, 'shop', $2, $3, $4, $5, $6, $7, '"
            + MODEL
            + "')",
            [(r[0], datetime.fromisoformat(r[1]), *r[2:]) for r in POD_ROWS],
        )
        await connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, owner_kind, owner_name) "
            "VALUES ($1, 'shop', $2, $3, $4, $5)",
            [(r[0], r[1], datetime.fromisoformat(r[2]), *r[3:]) for r in CONTAINER_ROWS],
        )
        assert await runner._run_in_transaction(connection) >= 1
        rows = await connection.fetch(
            "SELECT pod_name, timestamp, owner_kind, owner_name, cpu_request, memory_request, metadata_source, "
            "model_version FROM combined_metrics"
        )
        pods = {(r[0], r[1].isoformat()[11:16]): tuple(r[2:]) for r in rows}
        containers = [tuple(r) for r in await connection.fetch("SELECT owner_kind, owner_name FROM container_metrics")]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
    assert pods == EXPECTED_PODS
    assert containers == [("Deployment", "web")]
