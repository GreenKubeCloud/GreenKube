"""Readiness snapshot and administrator allowlist storage (migration 0071, roadmap F12/F2),
and migrations at controller startup only (F11)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.storage.automation_capability_repository import (
    SCOPE_GLOBAL,
    SCOPE_REPOSITORY,
    AutomationCapabilityRepository,
    CapabilityRow,
)


def _configure(backend: str, tmp_path, monkeypatch) -> tuple[str | None, str | None]:
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_capabilities_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
        return dsn, schema
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "capabilities.sqlite"))
    return None, None


async def _drop(dsn: str | None, schema: str | None) -> None:
    if dsn and schema:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    dsn, schema = _configure(request.param, tmp_path, monkeypatch)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        await _drop(dsn, schema)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


async def test_snapshot_is_replaced_as_a_whole(db):
    repo = AutomationCapabilityRepository(db)
    now = _now()
    await repo.replace_snapshot(
        [
            CapabilityRow(SCOPE_GLOBAL, "delivery", "dormant", {"reason": "credentials_missing"}),
            CapabilityRow(SCOPE_REPOSITORY, "https://github.com/acme/a.git", "dormant", {"repository": "acme/a"}),
            CapabilityRow(SCOPE_REPOSITORY, "https://github.com/acme/b.git", "dormant", {"repository": "acme/b"}),
        ],
        now=now,
    )
    later = now + timedelta(minutes=1)
    await repo.replace_snapshot(
        [
            CapabilityRow(SCOPE_GLOBAL, "delivery", "active", {"reason": None}),
            CapabilityRow(SCOPE_REPOSITORY, "https://github.com/acme/a.git", "active", {"repository": "acme/a"}),
        ],
        now=later,
    )

    rows = await repo.list_capabilities()
    assert [(row.scope, row.subject, row.state) for row in rows] == [
        (SCOPE_GLOBAL, "delivery", "active"),
        (SCOPE_REPOSITORY, "https://github.com/acme/a.git", "active"),
    ]
    assert rows[0].payload == {"reason": None}
    assert rows[0].updated_at == later
    assert [row.subject for row in await repo.list_capabilities(SCOPE_REPOSITORY)] == ["https://github.com/acme/a.git"]


async def test_allowed_repositories_are_idempotent(db):
    repo = AutomationCapabilityRepository(db)
    entry = {"repo_url": "https://github.com/acme/a.git", "host": "github.com", "repository": "acme/a"}

    assert await repo.add_allowed_repositories([entry], actor="user:admin", now=_now()) == 1
    assert await repo.add_allowed_repositories([entry], actor="user:other", now=_now()) == 0

    (stored,) = await repo.list_allowed_repositories()
    assert stored.repository == "acme/a"
    assert stored.created_by == "user:admin"
    assert stored.created_at.tzinfo is not None


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_only_the_migrating_process_applies_migrations(backend, tmp_path, monkeypatch):
    """F11: the controller migrates (DB_RUN_MIGRATIONS=true); the API waits for it."""
    if backend == "sqlite":
        pytest.skip("SQLite always migrates: it is single-process local development.")
    dsn, schema = _configure(backend, tmp_path, monkeypatch)
    monkeypatch.setenv("DB_RUN_MIGRATIONS", "false")
    config.reload()
    api = DatabaseManager(Config())
    controller = None
    try:
        await api.connect()
        status = await api.schema_status()
        assert status.current is False
        assert status.expected_version == MigrationRunner("postgres").latest_version()
        assert status.pending[-1] == status.expected_version >= 71

        monkeypatch.setenv("DB_RUN_MIGRATIONS", "true")
        config.reload()
        controller = DatabaseManager(Config())
        await controller.connect()

        assert (await controller.schema_status()).current is True
        assert (await api.schema_status()).current is True
    finally:
        await api.close()
        if controller is not None:
            await controller.close()
        await _drop(dsn, schema)


async def test_sqlite_is_current_after_connect(tmp_path, monkeypatch):
    _configure("sqlite", tmp_path, monkeypatch)
    monkeypatch.setenv("DB_RUN_MIGRATIONS", "false")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        status = await manager.schema_status()
        assert status.current is True
        assert status.pending == ()
    finally:
        await manager.close()
