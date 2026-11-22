"""Migration 0310 retires the open recommendations of the removed connectors (roadmap B.3, B.0)."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner

#: (id, source, status): only the active rows of a removed connector are retired.
ROWS = [
    (1, "kubecost", "active"),
    (2, "polaris", "active"),
    (3, "gke", "active"),
    (4, "azure_advisor", "active"),
    (5, "kubecost", "pr_open"),
    (6, "kubecost", "verified"),
    (7, "greenkube", "active"),
    (8, "krr", "active"),
]
EXPECTED_STATUS = {
    1: "stale",
    2: "stale",
    3: "stale",
    4: "stale",
    5: "pr_open",
    6: "verified",
    7: "active",
    8: "active",
}


async def _migrate(path: str, *, up_to: int | None = None, after: int = 0) -> None:
    runner = MigrationRunner("sqlite")
    async with aiosqlite.connect(path) as connection:
        await runner._ensure_migrations_table(connection)
        for version, name, sql in runner._discover_scripts():
            if version <= after:
                continue
            if up_to is not None and version > up_to:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        await connection.commit()


def _assert_retired(statuses: dict, events: list) -> None:
    assert statuses == EXPECTED_STATUS
    payloads = {row_id: json.loads(payload) for row_id, payload in events}
    assert set(payloads) == {1, 2, 3, 4}
    assert {payload["reason"] for payload in payloads.values()} == {"type_removed"}
    assert payloads[4]["source"] == "azure_advisor"


async def test_active_recommendations_of_removed_connectors_are_retired(tmp_path):
    path = str(tmp_path / "upgrade.sqlite")
    await _migrate(path, up_to=309)
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
            "source, created_at) VALUES (?, 'api', 'shop', 'RIGHTSIZING_CPU', 'd', ?, ?, ?, '2026-11-01T00:00:00Z')",
            [(row_id, status, f"fp-{row_id}", source) for row_id, source, status in ROWS],
        )

    await _migrate(path, after=309)

    with sqlite3.connect(path) as db:
        statuses = dict(db.execute("SELECT id, status FROM recommendation_history ORDER BY id").fetchall())
        events = db.execute(
            "SELECT recommendation_id, payload FROM recommendation_events WHERE event_type = 'stale' "
            "ORDER BY recommendation_id"
        ).fetchall()
    _assert_retired(statuses, events)


async def test_active_recommendations_of_removed_connectors_are_retired_on_postgres():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL migration test.")
    schema = f"gk_0310_{uuid4().hex}"
    runner = MigrationRunner("postgres")
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await runner._ensure_migrations_table(connection)
        scripts = runner._discover_scripts()
        for version, name, sql in scripts:
            if version > 309:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        created = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for row_id, source, status in ROWS:
            await connection.execute(
                "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
                "source, created_at) VALUES ($1, 'api', 'shop', 'RIGHTSIZING_CPU', 'd', $2, $3, $4, $5)",
                row_id,
                status,
                f"fp-{row_id}",
                source,
                created,
            )
        for version, _name, sql in scripts:
            if version == 310:
                await runner._execute_sql(connection, sql)
        statuses = {
            row["id"]: row["status"] for row in await connection.fetch("SELECT id, status FROM recommendation_history")
        }
        events = [
            (row["recommendation_id"], row["payload"])
            for row in await connection.fetch("SELECT recommendation_id, payload FROM recommendation_events")
        ]
        _assert_retired(statuses, events)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
