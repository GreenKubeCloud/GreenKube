"""Repair of stored pod metadata gaps by migration 0270 (COL-01 step 6, roadmap D.2).

Rows written from the Kubernetes state at collection time (``metadata_source``
NULL) or without any pod metadata (``missing``) can carry owner NULL and
requests 0 while other rows of the same pod know them. The migration copies
the owner and the last known requests from those rows, within one continuous
run of rows of the same pod incarnation, marks the repaired rows
``metadata_source = repaired`` (``repaired_partial`` when the source had no
owner) and their ``model_version`` as outdated, so ``greenkube recompute``
re-runs their allocation, and gives them new row ids so the workload usage
rollup re-rolls their hours. Its cost is linear in the table size.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

PREVIOUS_VERSION = 269
MODEL = "impact-1"
REPAIRED = MODEL + "+metadata-repaired"

POD_ROWS = [
    # pod_name, timestamp, owner_kind, owner_name, cpu_request, memory_request, metadata_source
    ("web-1", "2026-10-01T10:00:00+00:00", "Deployment", "web", 500, 256, "k8s_snapshot"),
    ("web-1", "2026-10-01T10:05:00+00:00", "Deployment", "web", 300, 128, "k8s_snapshot"),
    ("web-1", "2026-10-01T10:10:00+00:00", None, None, 0, 0, "missing"),
    ("web-1", "2026-10-01T10:15:00+00:00", None, None, 0, 0, None),
    ("early-1", "2026-10-01T09:55:00+00:00", None, None, 0, 0, "missing"),
    ("early-1", "2026-10-01T10:00:00+00:00", "Job", "early", 100, 64, "kube_state_metrics"),
    ("bare-1", "2026-10-01T10:00:00+00:00", None, None, 0, 0, "missing"),
    # An unobserved hour between the rows: possibly another incarnation, never repaired across it.
    ("gap-1", "2026-10-01T10:00:00+00:00", "StatefulSet", "gap", 200, 64, "k8s_snapshot"),
    ("gap-1", "2026-10-01T11:00:00+00:00", None, None, 0, 0, "missing"),
    # The only source has requests but no owner: a partial repair.
    ("part-1", "2026-10-01T10:00:00+00:00", None, None, 50, 0, None),
    ("part-1", "2026-10-01T10:05:00+00:00", None, None, 0, 0, "missing"),
    # A new pod_snapshots version (another incarnation) starts between the rows.
    ("sts-0", "2026-10-01T10:00:00+00:00", "StatefulSet", "sts", 400, 100, "k8s_snapshot"),
    ("sts-0", "2026-10-01T10:05:00+00:00", None, None, 0, 0, "missing"),
    # A BestEffort pod: owner known, requests 0, nothing to repair.
    ("best-1", "2026-10-01T10:00:00+00:00", "Deployment", "best", 0, 0, None),
]
SNAPSHOTS = [
    # pod_name, pod_uid, first_seen, last_seen
    ("sts-0", "uid-a", "2026-10-01T09:59:00+00:00", "2026-10-01T10:01:00+00:00"),
    ("sts-0", "uid-b", "2026-10-01T10:02:00+00:00", "2026-10-01T10:06:00+00:00"),
]
CONTAINER_ROWS = [
    # pod_name, container_name, timestamp, owner_kind, owner_name
    ("web-1", "app", "2026-10-01T10:15:00+00:00", None, None),
]

EXPECTED_PODS = {
    ("web-1", "10:00"): ("Deployment", "web", 500, 256, "k8s_snapshot", MODEL),
    ("web-1", "10:05"): ("Deployment", "web", 300, 128, "k8s_snapshot", MODEL),
    # Last known requests (10:05), not the first ones.
    ("web-1", "10:10"): ("Deployment", "web", 300, 128, "repaired", REPAIRED),
    ("web-1", "10:15"): ("Deployment", "web", 300, 128, "repaired", REPAIRED),
    # No earlier row: the nearest later one.
    ("early-1", "09:55"): ("Job", "early", 100, 64, "repaired", REPAIRED),
    ("early-1", "10:00"): ("Job", "early", 100, 64, "kube_state_metrics", MODEL),
    # Nothing to repair from: unchanged.
    ("bare-1", "10:00"): (None, None, 0, 0, "missing", MODEL),
    ("gap-1", "10:00"): ("StatefulSet", "gap", 200, 64, "k8s_snapshot", MODEL),
    ("gap-1", "11:00"): (None, None, 0, 0, "missing", MODEL),
    ("part-1", "10:00"): (None, None, 50, 0, None, MODEL),
    ("part-1", "10:05"): (None, None, 50, 0, "repaired_partial", REPAIRED),
    ("sts-0", "10:00"): ("StatefulSet", "sts", 400, 100, "k8s_snapshot", MODEL),
    ("sts-0", "10:05"): (None, None, 0, 0, "missing", MODEL),
    ("best-1", "10:00"): ("Deployment", "best", 0, 0, None, MODEL),
}
REPAIRED_ROWS = {key for key, value in EXPECTED_PODS.items() if value[-1] == REPAIRED}


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


def _insert_sqlite(connection: sqlite3.Connection, pod_rows) -> None:
    connection.executemany(
        'INSERT INTO combined_metrics (pod_name, namespace, "timestamp", owner_kind, owner_name, cpu_request, '
        "memory_request, metadata_source, model_version) VALUES (?, 'shop', ?, ?, ?, ?, ?, ?, '" + MODEL + "')",
        pod_rows,
    )


async def test_sqlite_migration_repairs_pod_metadata_gaps(tmp_path):
    db_file = str(tmp_path / "gaps.db")
    await _sqlite_at_previous_version(db_file)
    with sqlite3.connect(db_file) as connection:
        _insert_sqlite(connection, POD_ROWS)
        connection.executemany(
            "INSERT INTO pod_snapshots (namespace, pod_name, pod_uid, spec_hash, containers, first_seen, last_seen) "
            "VALUES ('shop', ?, ?, 'h', '[]', ?, ?)",
            SNAPSHOTS,
        )
        connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, owner_kind, owner_name) "
            "VALUES (?, 'shop', ?, ?, ?, ?)",
            CONTAINER_ROWS,
        )
        max_id_before = connection.execute("SELECT MAX(id) FROM combined_metrics").fetchone()[0]

    async with aiosqlite.connect(db_file) as connection:
        assert await MigrationRunner("sqlite").run(connection) >= 1

    with sqlite3.connect(db_file) as connection:
        rows = list(
            connection.execute(
                'SELECT pod_name, "timestamp", owner_kind, owner_name, cpu_request, memory_request, '
                "metadata_source, model_version, id FROM combined_metrics"
            )
        )
        containers = list(connection.execute("SELECT owner_kind, owner_name FROM container_metrics"))
        # The next insert still gets a fresh id after the renumbering.
        _insert_sqlite(connection, [("late-1", "2026-10-01T12:00:00+00:00", None, None, 0, 0, None)])
        late_id = connection.execute("SELECT id FROM combined_metrics WHERE pod_name = 'late-1'").fetchone()[0]
    pods = {(row[0], _hhmm(row[1])): tuple(row[2:8]) for row in rows}
    assert pods == EXPECTED_PODS
    assert containers == [("Deployment", "web")]
    # Repaired rows get ids above the previous maximum: the usage rollup re-rolls their hours.
    ids = {(row[0], _hhmm(row[1])): row[8] for row in rows}
    assert all(ids[key] > max_id_before for key in REPAIRED_ROWS)
    assert all(ids[key] <= max_id_before for key in set(ids) - REPAIRED_ROWS)
    assert late_id > max(ids.values())


async def test_sqlite_migration_cost_is_linear(tmp_path):
    """Review 2: 20 pods x 2000 rows (BestEffort and repairable) cost seconds, not minutes."""
    db_file = str(tmp_path / "large.db")
    await _sqlite_at_previous_version(db_file)
    rows = []
    for pod in range(20):
        for step in range(2000):
            hour, minute = divmod(step * 5, 60)
            ts = f"2026-10-{1 + hour // 24:02d}T{hour % 24:02d}:{minute:02d}:00+00:00"
            if pod % 2:
                rows.append((f"best-{pod}", ts, "Deployment", "best", 0, 0, None))
            elif step % 2:
                rows.append((f"web-{pod}", ts, None, None, 0, 0, "missing"))
            else:
                rows.append((f"web-{pod}", ts, "Deployment", "web", 100, 64, "k8s_snapshot"))
    with sqlite3.connect(db_file) as connection:
        _insert_sqlite(connection, rows)

    started = time.perf_counter()
    async with aiosqlite.connect(db_file) as connection:
        assert await MigrationRunner("sqlite").run(connection) >= 1
    elapsed = time.perf_counter() - started

    with sqlite3.connect(db_file) as connection:
        repaired = connection.execute(
            "SELECT COUNT(*) FROM combined_metrics WHERE metadata_source = 'repaired'"
        ).fetchone()[0]
    assert repaired == 10 * 1000
    assert elapsed < 5.0, elapsed


async def test_postgres_migration_repairs_pod_metadata_gaps():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration upgrade tests.")

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
            "INSERT INTO pod_snapshots (namespace, pod_name, pod_uid, spec_hash, containers, first_seen, last_seen) "
            "VALUES ('shop', $1, $2, 'h', '[]'::jsonb, $3, $4)",
            [(r[0], r[1], datetime.fromisoformat(r[2]), datetime.fromisoformat(r[3])) for r in SNAPSHOTS],
        )
        await connection.executemany(
            "INSERT INTO container_metrics (pod_name, namespace, container_name, timestamp, owner_kind, owner_name) "
            "VALUES ($1, 'shop', $2, $3, $4, $5)",
            [(r[0], r[1], datetime.fromisoformat(r[2]), *r[3:]) for r in CONTAINER_ROWS],
        )
        max_id_before = await connection.fetchval("SELECT MAX(id) FROM combined_metrics")
        assert await runner._run_in_transaction(connection) >= 1
        rows = await connection.fetch(
            "SELECT pod_name, timestamp, owner_kind, owner_name, cpu_request, memory_request, metadata_source, "
            "model_version, id FROM combined_metrics"
        )
        containers = [tuple(r) for r in await connection.fetch("SELECT owner_kind, owner_name FROM container_metrics")]
        late_id = await connection.fetchval(
            "INSERT INTO combined_metrics (pod_name, namespace, timestamp) "
            "VALUES ('late-1', 'shop', now()) RETURNING id"
        )
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
    pods = {(r[0], r[1].isoformat()[11:16]): tuple(r[2:8]) for r in rows}
    assert pods == EXPECTED_PODS
    assert containers == [("Deployment", "web")]
    ids = {(r[0], r[1].isoformat()[11:16]): r[8] for r in rows}
    assert all(ids[key] > max_id_before for key in REPAIRED_ROWS)
    assert late_id > max(ids.values())
