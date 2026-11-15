"""Migration 0132 merges duplicate open recommendations before indexing them (REC-06, SQLite)."""

import sqlite3

import aiosqlite
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
        (1, "A", "ignored"),
        (2, "A", "active"),
        # fingerprint B: two ignored rows (keep the latest) and an older stale row.
        (3, "B", "ignored"),
        (4, "B", "ignored"),
        (5, "B", "stale"),
        # fingerprint C: an active row behind an open pull request of the same advice.
        (6, "C", "pr_open"),
        (7, "C", "active"),
    ]
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
            "created_at) VALUES (?, 'api', 'shop', 'RIGHTSIZING_CPU', 'd', ?, ?, '2026-11-01T00:00:00Z')",
            [(row_id, status, fingerprint) for row_id, fingerprint, status in rows],
        )

    await _migrate(path, after=131)

    with sqlite3.connect(path) as db:
        statuses = dict(db.execute("SELECT id, status FROM recommendation_history ORDER BY id").fetchall())
        events = db.execute(
            "SELECT recommendation_id FROM recommendation_events WHERE event_type = 'stale' ORDER BY recommendation_id"
        ).fetchall()
        assert statuses == {1: "ignored", 2: "stale", 3: "stale", 4: "ignored", 5: "stale", 6: "pr_open", 7: "stale"}
        assert [row[0] for row in events] == [2, 3, 7]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO recommendation_history (pod_name, namespace, type, description, status, fingerprint, "
                "created_at) VALUES ('api', 'shop', 'RIGHTSIZING_CPU', 'd', 'active', 'A', '2026-11-02T00:00:00Z')"
            )
