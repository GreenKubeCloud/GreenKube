"""``source_runs`` and ``source_signals`` on SQLite and PostgreSQL (roadmap E5, migrations 0065/0066)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    DeletionProposal,
    SignalProvenance,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.runtime import SourceRunRecord, SourceRunStatus
from greenkube.signals.sdk import Diagnostic, SourceHealthStatus
from greenkube.storage.base_signal_repository import create_signal_repository


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_signals_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "signals.sqlite"))
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
    return create_signal_repository(manager, manager.db_type)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _run(
    source_id: str, started: datetime, status: SourceRunStatus = SourceRunStatus.SUCCEEDED, **kw
) -> SourceRunRecord:
    values = dict(
        source_id=source_id,
        status=status,
        complete=status == SourceRunStatus.SUCCEEDED,
        started_at=started,
        finished_at=started + timedelta(seconds=2),
        duration_seconds=2.0,
    )
    values.update(kw)
    return SourceRunRecord(**values)


def _signal(source_id: str, name: str, reason: str = "Released", fingerprint: str | None = None) -> SourceSignal:
    return SourceSignal(
        source_id=source_id,
        target=TargetRef.for_kind("c1", "PersistentVolume", name),
        capability=Capability.CLEANUP,
        kind="ORPHANED_PERSISTENT_VOLUME",
        proposal=DeletionProposal(reason=reason),
        algorithm=AlgorithmInfo(name=source_id),
        provenance=SignalProvenance(legacy_source="greenkube", legacy_fingerprint=fingerprint),
    )


async def test_record_run_round_trips_every_field(db):
    repo = _repo(db)
    started = _now() - timedelta(minutes=5)
    run = _run(
        "vpa",
        started,
        optimization_run_id=42,
        source_version="1.0",
        namespace="shop",
        health_status=SourceHealthStatus.DEGRADED,
        health_reason="slow API",
        signal_count=1,
        recommendation_count=2,
        consecutive_failures=0,
        last_collected_at=started,
        error_code=None,
        diagnostics=[Diagnostic(level="warning", code="skipped_vpas", message="updateMode=Auto", count=3)],
    )

    stored = await repo.record_run(run, [_signal("vpa", "pv-1")])

    assert stored.id is not None
    [listed] = await repo.list_runs("vpa")
    assert listed == stored


async def test_signals_are_stored_once_and_their_sighting_moves_forward(db):
    repo = _repo(db)
    first_at = _now() - timedelta(hours=2)
    first = await repo.record_run(_run("greenkube.orphaned_pv", first_at), [_signal("greenkube.orphaned_pv", "pv-1")])
    second = await repo.record_run(
        _run("greenkube.orphaned_pv", first_at + timedelta(hours=1)),
        [
            _signal("greenkube.orphaned_pv", "pv-1", fingerprint="v3:cluster::pv-1"),
            _signal("greenkube.orphaned_pv", "pv-2"),
        ],
    )

    stored = await repo.list_signals("greenkube.orphaned_pv")
    assert len(stored) == 2
    by_name = {item.signal.target.name: item for item in stored}
    pv1 = by_name["pv-1"]
    assert pv1.first_seen_at == first.finished_at
    assert pv1.last_seen_at == second.finished_at
    assert pv1.last_run_id == second.id
    assert pv1.recommendation_fingerprint == "v3:cluster::pv-1"
    expected = _signal("greenkube.orphaned_pv", "pv-1", fingerprint="v3:cluster::pv-1")
    assert pv1.signal.signal_id == expected.signal_id
    assert pv1.signal.provenance == expected.provenance

    [only] = await repo.list_signals("greenkube.orphaned_pv", target_key="c1/PersistentVolume/_/pv-2")
    assert only.signal.target.name == "pv-2"

    # A reworded reason is the same advice: the row is updated, not duplicated.
    await repo.record_run(
        _run("greenkube.orphaned_pv", first_at + timedelta(hours=2)),
        [_signal("greenkube.orphaned_pv", "pv-1", reason="Released for 30 days")],
    )
    [reworded] = await repo.list_signals("greenkube.orphaned_pv", target_key="c1/PersistentVolume/_/pv-1")
    assert reworded.first_seen_at == first.finished_at
    proposal = reworded.signal.proposal
    assert isinstance(proposal, DeletionProposal) and proposal.reason == "Released for 30 days"


async def test_latest_runs_and_last_success_per_source(db):
    repo = _repo(db)
    base = _now() - timedelta(hours=3)
    await repo.record_run(_run("vpa", base))
    await repo.record_run(
        _run("vpa", base + timedelta(hours=1), SourceRunStatus.FAILED, error_code="timeout", consecutive_failures=1)
    )
    await repo.record_run(_run("greenkube.zombie", base + timedelta(hours=2)))

    summaries = await repo.latest_runs()

    assert set(summaries) == {"vpa", "greenkube.zombie"}
    vpa = summaries["vpa"]
    assert vpa.latest.status == SourceRunStatus.FAILED
    assert vpa.latest.consecutive_failures == 1
    assert vpa.last_success_at == base + timedelta(seconds=2)
    assert summaries["greenkube.zombie"].last_success_at is not None


async def test_list_runs_is_newest_first_and_bounded(db):
    repo = _repo(db)
    base = _now() - timedelta(hours=5)
    for hour in range(4):
        await repo.record_run(_run("vpa", base + timedelta(hours=hour)))
    runs = await repo.list_runs("vpa", limit=2)
    assert [run.started_at for run in runs] == [base + timedelta(hours=3), base + timedelta(hours=2)]
    assert await repo.list_runs("unknown") == []


async def test_prune_deletes_old_runs_and_signals_only(db):
    repo = _repo(db)
    old = _now() - timedelta(days=40)
    recent = _now() - timedelta(days=1)
    await repo.record_run(_run("vpa", old), [_signal("vpa", "pv-old")])
    await repo.record_run(_run("vpa", recent), [_signal("vpa", "pv-new")])

    deleted = await repo.prune(_now() - timedelta(days=30))

    assert deleted == 2
    assert [run.started_at for run in await repo.list_runs("vpa")] == [recent]
    assert [item.signal.target.name for item in await repo.list_signals("vpa")] == ["pv-new"]
