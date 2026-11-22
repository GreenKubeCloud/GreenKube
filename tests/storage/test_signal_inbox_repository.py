"""Inbox of pushed signals on SQLite and PostgreSQL (roadmap G2, G6; migration 0070)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.signals.connectors.inbox import InboxBatch, create_signal_inbox_repository
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    DeletionProposal,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.sdk import Diagnostic


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_inbox_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "inbox.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repo(manager: DatabaseManager):
    return create_signal_inbox_repository(manager, manager.db_type)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _signal(source_id: str, name: str, namespace: str = "shop") -> SourceSignal:
    return SourceSignal(
        source_id=source_id,
        target=TargetRef.for_kind("c1", "Deployment", name, namespace=namespace),
        capability=Capability.CLEANUP,
        kind="ZOMBIE_POD",
        proposal=DeletionProposal(reason="idle"),
        algorithm=AlgorithmInfo(name="acme"),
        provenance=SignalProvenance(origin=SignalOrigin.PUSH),
    )


def _batch(source_id: str, received_at: datetime, complete: bool, count: int, batch_id=None) -> InboxBatch:
    return InboxBatch(
        batch_id=batch_id or uuid4().hex,
        source_id=source_id,
        received_at=received_at,
        complete=complete,
        signal_count=count,
        pushed_by="apikey:ci",
        diagnostics=[Diagnostic(code="duplicate_signal", message="merged", count=1)],
    )


async def test_accept_is_idempotent_on_signal_id(db):
    repo = _repo(db)
    now = _now()
    signals = [_signal("push:ci", "a"), _signal("push:ci", "b")]

    await repo.accept(_batch("push:ci", now - timedelta(minutes=5), False, 2), signals)
    await repo.accept(_batch("push:ci", now, False, 2), signals)

    current = await repo.current(source_id="push:ci")
    assert sorted(item.signal.target.name for item in current) == ["a", "b"]
    assert all(item.received_at == now for item in current)
    assert await repo.count("push:ci") == 2


async def test_incomplete_batch_keeps_and_complete_batch_withdraws(db):
    repo = _repo(db)
    now = _now()
    await repo.accept(
        _batch("push:ci", now - timedelta(minutes=10), True, 2), [_signal("push:ci", "a"), _signal("push:ci", "b")]
    )
    partial = await repo.accept(_batch("push:ci", now - timedelta(minutes=5), False, 1), [_signal("push:ci", "c")])
    assert partial.withdrawn == 0
    assert await repo.count("push:ci") == 3

    full = await repo.accept(_batch("push:ci", now, True, 1), [_signal("push:ci", "b")])

    assert full.withdrawn == 2
    assert [item.signal.target.name for item in await repo.current(source_id="push:ci")] == ["b"]


async def test_complete_batch_only_touches_its_own_source(db):
    repo = _repo(db)
    now = _now()
    await repo.accept(_batch("push:ci", now, True, 1), [_signal("push:ci", "a")])
    await repo.accept(_batch("krr", now, True, 1), [_signal("krr", "a")])
    await repo.accept(_batch("push:ci", now + timedelta(seconds=1), True, 0), [])

    assert await repo.count("push:ci") == 0
    assert await repo.count("krr") == 1


async def test_current_filters_by_prefix_age_and_namespace(db):
    repo = _repo(db)
    now = _now()
    await repo.accept(_batch("push:ci", now - timedelta(days=3), False, 1), [_signal("push:ci", "old")])
    await repo.accept(_batch("push:ci", now, False, 1), [_signal("push:ci", "new", namespace="billing")])
    await repo.accept(_batch("push:bot", now, False, 1), [_signal("push:bot", "x")])
    await repo.accept(_batch("pushy", now, False, 1), [_signal("pushy", "y")])
    await repo.accept(_batch("krr", now, False, 1), [_signal("krr", "z")])

    pushed = await repo.current(source_prefix="push:")
    assert sorted(item.signal.source_id for item in pushed) == ["push:bot", "push:ci", "push:ci"]
    recent = await repo.current(source_prefix="push:", since=now - timedelta(days=1))
    assert sorted(item.signal.target.name for item in recent) == ["new", "x"]
    billing = await repo.current(source_prefix="push:", namespace="billing")
    assert [item.signal.target.name for item in billing] == ["new"]


async def test_latest_batches_per_source(db):
    repo = _repo(db)
    now = _now()
    await repo.accept(_batch("push:ci", now - timedelta(hours=1), True, 1), [_signal("push:ci", "a")])
    await repo.accept(_batch("push:ci", now, False, 1), [_signal("push:ci", "b")])
    await repo.accept(_batch("krr", now, True, 1), [_signal("krr", "a")])

    latest = await repo.latest_batches(source_prefix="push:")
    assert list(latest) == ["push:ci"]
    batch = latest["push:ci"]
    assert batch.received_at == now and batch.complete is False and batch.signal_count == 1
    assert batch.pushed_by == "apikey:ci" and batch.diagnostics[0].code == "duplicate_signal"
    assert list(await repo.latest_batches(source_id="krr")) == ["krr"]


async def test_prune_removes_old_rows(db):
    repo = _repo(db)
    now = _now()
    await repo.accept(_batch("push:ci", now - timedelta(days=40), False, 1), [_signal("push:ci", "old")])
    await repo.accept(_batch("push:ci", now, False, 1), [_signal("push:ci", "new")])

    removed = await repo.prune(now - timedelta(days=30))

    assert removed == 2
    assert [item.signal.target.name for item in await repo.current(source_id="push:ci")] == ["new"]
