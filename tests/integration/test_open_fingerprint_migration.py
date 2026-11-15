"""Migration 0132 merges duplicate open recommendations before indexing them (REC-06, SQLite)."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.migrations import MigrationRunner


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


async def test_duplicates_are_merged_and_then_impossible(tmp_path):
    path = str(tmp_path / "upgrade.sqlite")
    await _migrate(path, up_to=131)
    rows = [
        # fingerprint A: an ignored row and the active twin a later scan inserted.
        (1, "A", "ignored", "2026-11-02T00:00:00Z"),
        (2, "A", "active", None),
        # fingerprint B: two ignored rows (keep the one ignored last, here the older id) and a stale row.
        (3, "B", "ignored", "2026-11-05T00:00:00Z"),
        (4, "B", "ignored", "2026-11-03T00:00:00Z"),
        (5, "B", "stale", None),
        # fingerprint C: an active row behind an open pull request of the same advice.
        (6, "C", "pr_open", None),
        (7, "C", "active", None),
    ]
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
            "ignored_at, created_at) "
            "VALUES (?, 'api', 'shop', 'RIGHTSIZING_CPU', 'd', ?, ?, ?, '2026-11-01T00:00:00Z')",
            [(row_id, status, fingerprint, ignored_at) for row_id, fingerprint, status, ignored_at in rows],
        )

    await _migrate(path, after=131)

    with sqlite3.connect(path) as db:
        statuses = dict(db.execute("SELECT id, status FROM recommendation_history ORDER BY id").fetchall())
        events = db.execute(
            "SELECT recommendation_id, payload FROM recommendation_events WHERE event_type = 'stale' "
            "ORDER BY recommendation_id"
        ).fetchall()
        assert statuses == {1: "ignored", 2: "stale", 3: "ignored", 4: "stale", 5: "stale", 6: "pr_open", 7: "stale"}
        reasons = {row_id: json.loads(payload)["reason"] for row_id, payload in events}
        assert reasons == {2: "superseded_by_ignored", 4: "superseded_by_ignored", 7: "superseded_by_pr"}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO recommendation_history (pod_name, namespace, type, description, status, fingerprint, "
                "created_at) VALUES ('api', 'shop', 'RIGHTSIZING_CPU', 'd', 'active', 'A', '2026-11-02T00:00:00Z')"
            )


async def test_duplicates_are_merged_on_postgres():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL migration test.")
    schema = f"gk_0132_{uuid4().hex}"
    runner = MigrationRunner("postgres")
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await runner._ensure_migrations_table(connection)
        scripts = runner._discover_scripts()
        for version, name, sql in scripts:
            if version > 131:
                break
            await runner._execute_sql(connection, sql)
            await runner._record_migration(connection, version, name)
        created = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for row_id, fingerprint, status, ignored_day in (
            (1, "A", "ignored", 2),
            (2, "A", "active", None),
            (3, "B", "ignored", 5),
            (4, "B", "ignored", 3),
            (6, "C", "pr_open", None),
            (7, "C", "active", None),
        ):
            await connection.execute(
                "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
                "ignored_at, created_at) VALUES ($1, 'api', 'shop', 'RIGHTSIZING_CPU', 'd', $2, $3, $4, $5)",
                row_id,
                status,
                fingerprint,
                created.replace(day=ignored_day) if ignored_day else None,
                created,
            )
        for version, _name, sql in scripts:
            if version == 132:
                await runner._execute_sql(connection, sql)
        statuses = {
            row["id"]: row["status"] for row in await connection.fetch("SELECT id, status FROM recommendation_history")
        }
        reasons = {
            row["recommendation_id"]: json.loads(row["payload"])["reason"]
            for row in await connection.fetch("SELECT recommendation_id, payload FROM recommendation_events")
        }
        assert statuses == {1: "ignored", 2: "stale", 3: "ignored", 4: "stale", 6: "pr_open", 7: "stale"}
        assert reasons == {2: "superseded_by_ignored", 4: "superseded_by_ignored", 7: "superseded_by_pr"}
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
