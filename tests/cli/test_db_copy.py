"""``greenkube db copy``: the SQLite to PostgreSQL upgrade path of 0.3.x installs (F17)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import typer
from typer.testing import CliRunner

from greenkube.cli import db as db_cli
from greenkube.cli.main import app


def test_sqlite_url_and_target_validation(tmp_path):
    assert db_cli.sqlite_path_from_url("sqlite:///data/greenkube_data.db").as_posix() == "/data/greenkube_data.db"
    with pytest.raises(typer.BadParameter):
        db_cli.sqlite_path_from_url("postgresql://x")
    result = CliRunner().invoke(app, ["db", "copy", "--from", "sqlite:///x.db", "--to", "mysql://x"])
    assert result.exit_code != 0


def test_caches_are_never_copied_and_metrics_only_on_request():
    source = ["recommendation_history", "combined_metrics", "metrics_summary", "only_in_source"]
    target = ["recommendation_history", "combined_metrics", "metrics_summary", "gitops_providers"]

    assert db_cli.select_tables(source, target, include_metrics=False) == (
        ["recommendation_history"],
        ["combined_metrics", "metrics_summary"],
    )
    assert db_cli.select_tables(source, target, include_metrics=True) == (
        ["combined_metrics", "recommendation_history"],
        ["metrics_summary"],
    )


def test_parents_are_copied_before_children():
    tables = ["recommendation_events", "recommendation_history", "automation_plans", "automation_plan_files"]
    references = [
        ("recommendation_events", "recommendation_history"),
        ("automation_plan_files", "automation_plans"),
        ("automation_plans", "automation_plans"),
    ]

    order = db_cli.order_by_foreign_keys(tables, references)

    assert order.index("recommendation_history") < order.index("recommendation_events")
    assert order.index("automation_plans") < order.index("automation_plan_files")


@pytest.mark.parametrize(
    ("value", "data_type", "expected"),
    [
        ("2026-01-02T03:04:05Z", "timestamp with time zone", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        ("2026-01-02 03:04:05", "timestamp with time zone", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        ("2026-01-02T03:04:05+00:00", "timestamp without time zone", datetime(2026, 1, 2, 3, 4, 5)),
        (1, "boolean", True),
        ("0", "boolean", False),
        ("12", "bigint", 12),
        (1, "double precision", 1.0),
        (0.1, "numeric", Decimal("0.1")),
        ({"a": 1}, "jsonb", '{"a": 1}'),
        ('{"a": 1}', "json", '{"a": 1}'),
        (5, "text", "5"),
        (None, "boolean", None),
    ],
)
def test_values_are_converted_to_postgres_types(value, data_type, expected):
    assert db_cli.convert_value(value, data_type) == expected


# --------------------------------------------------------------------------- live PostgreSQL


@pytest.fixture
async def target():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL copy test.")
    schema = f"gk_copy_{uuid4().hex}"
    yield dsn, schema
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        await conn.close()


async def _sqlite_source(path) -> None:
    """A SQLite database as 0.3.x wrote it: current schema, rows in the tables an upgrade must keep."""
    import aiosqlite

    from greenkube.core.migrations import MigrationRunner

    created = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    async with aiosqlite.connect(path) as conn:
        await MigrationRunner("sqlite").run(conn)
        for rec_id in (1, 2, 3):
            await conn.execute(
                "INSERT INTO recommendation_history (id, type, description, created_at) VALUES (?, ?, ?, ?)",
                (rec_id, "RIGHTSIZING_CPU", f"rec {rec_id}", created),
            )
            await conn.execute(
                "INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at) "
                "VALUES (?, 'created', 'system', '{}', ?)",
                (rec_id, created),
            )
        await conn.execute(
            "INSERT INTO gitops_providers (id, provider_type, enabled) VALUES ('github-main', 'github', 1)"
        )
        await conn.commit()
    # A WAL-mode writer may still hold rows outside the main file: the copy must see them.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")


async def test_copies_state_once_and_is_idempotent(tmp_path, target):
    dsn, schema = target
    source = tmp_path / "greenkube_data.db"
    await _sqlite_source(source)
    before = source.read_bytes()

    report = await db_cli.copy_sqlite_to_postgres(source, dsn, schema=schema, work_dir=tmp_path)

    copied = {table.table: table.copied for table in report.tables}
    assert copied["recommendation_history"] == 3
    assert copied["recommendation_events"] == 3
    assert copied["gitops_providers"] == 1
    assert "combined_metrics" in report.excluded
    assert source.read_bytes() == before, "the source database must never be written"

    conn = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        provider = await conn.fetchrow("SELECT provider_type, enabled FROM gitops_providers WHERE id = 'github-main'")
        assert provider["enabled"] is True
        created = await conn.fetchval("SELECT created_at FROM recommendation_history WHERE id = 1")
        assert created is not None
        # Identity sequences moved past the copied ids: a new recommendation does not collide.
        new_id = await conn.fetchval(
            "INSERT INTO recommendation_history (type, description, created_at) "
            "VALUES ('RIGHTSIZING_CPU', 'new', NOW()) RETURNING id"
        )
        assert new_id > 3
    finally:
        await conn.close()

    rerun = await db_cli.copy_sqlite_to_postgres(source, dsn, schema=schema, work_dir=tmp_path)
    assert rerun.copied == 0
    assert {table.table: table.skipped for table in rerun.tables}["recommendation_history"] == 3


async def test_dry_run_writes_nothing(tmp_path, target):
    dsn, schema = target
    source = tmp_path / "greenkube_data.db"
    await _sqlite_source(source)

    report = await db_cli.copy_sqlite_to_postgres(source, dsn, schema=schema, dry_run=True, work_dir=tmp_path)

    assert {table.table: table.source_rows for table in report.tables}["recommendation_history"] == 3
    assert report.copied == 0
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass($1)", f"{schema}.recommendation_history")
    finally:
        await conn.close()
    assert exists is None
