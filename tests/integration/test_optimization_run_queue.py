"""Contract tests for the optimization run queue on SQLite and PostgreSQL.

Covers the run request queue (E1), run outcome persistence (E10) and the
0045 upgrade from existing data. PostgreSQL cases run when
``GREENKUBE_TEST_POSTGRES_DSN`` is set.
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Optional
from uuid import uuid4

import aiosqlite
import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.migrations import MigrationRunner
from greenkube.models.optimization_run import (
    OptimizationRunCompletion,
    OptimizationRunDiff,
    OptimizationRunStatus,
    OptimizationRunTrigger,
    SourceRunOutcome,
    SourceRunStatus,
)
from greenkube.storage.postgres.optimization_run_repository import PostgresOptimizationRunRepository
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository

T0 = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
QUEUE_MIGRATION = 45


@dataclass
class Backend:
    name: str
    managers: list
    schema: Optional[str] = None
    dsn: Optional[str] = None

    def runs(self, index: int = 0):
        manager = self.managers[index]
        if self.name == "postgres":
            return PostgresOptimizationRunRepository(manager)
        return SQLiteOptimizationRunRepository(manager)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def backend(request, tmp_path, monkeypatch) -> AsyncGenerator[Backend, None]:
    """Two independent connections (``managers``) to one database, like two processes."""
    name = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if name == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL optimization run contract tests.")
        schema = f"gk_runs_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "runs.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    managers = [DatabaseManager(Config()), DatabaseManager(Config())]
    for manager in managers:
        await manager.connect()
    try:
        yield Backend(name=name, managers=managers, schema=schema, dsn=dsn)
    finally:
        for manager in managers:
            await manager.close()
        if name == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def _enqueue(repo, namespace=None, trigger=OptimizationRunTrigger.MANUAL, at=T0, by="tester"):
    return await repo.enqueue(namespace=namespace, trigger=trigger, requested_at=at, requested_by=by)


# --------------------------------------------------------------------------- #
# Queue (E1)                                                                   #
# --------------------------------------------------------------------------- #


async def test_enqueue_deduplicates_while_a_run_is_pending(backend):
    repo = backend.runs()

    first, created = await _enqueue(repo)
    again, created_again = await _enqueue(backend.runs(1), trigger=OptimizationRunTrigger.REFRESH)

    assert created is True
    assert first.status == OptimizationRunStatus.QUEUED
    assert first.trigger == OptimizationRunTrigger.MANUAL
    assert first.requested_by == "tester"
    assert first.started_at is None
    assert created_again is False
    assert again.id == first.id


async def test_pending_cluster_run_covers_namespaced_requests(backend):
    repo = backend.runs()
    cluster, _ = await _enqueue(repo)

    namespaced, created = await _enqueue(repo, namespace="prod")

    assert created is False
    assert namespaced.id == cluster.id


async def test_namespaced_requests_deduplicate_per_namespace(backend):
    repo = backend.runs()
    prod, _ = await _enqueue(repo, namespace="prod")
    prod_again, created_again = await _enqueue(repo, namespace="prod")
    staging, created_staging = await _enqueue(repo, namespace="staging")

    assert created_again is False and prod_again.id == prod.id
    assert created_staging is True and staging.id != prod.id


async def test_a_new_request_is_queued_once_the_pending_run_is_claimed(backend):
    repo = backend.runs()
    first, _ = await _enqueue(repo)
    claimed = await repo.claim_next(claimed_by="controller-0", started_at=T0)

    second, created = await _enqueue(repo, at=T0 + timedelta(seconds=1))

    assert claimed is not None and claimed.id == first.id
    assert created is True
    assert second.id != first.id


async def test_claim_hands_each_queued_run_to_exactly_one_process(backend):
    await _enqueue(backend.runs(), namespace="a", at=T0)
    await _enqueue(backend.runs(), namespace="b", at=T0 + timedelta(seconds=1))

    claims = await asyncio.gather(
        *(backend.runs(i % 2).claim_next(claimed_by=f"controller-{i}", started_at=T0) for i in range(6))
    )
    claimed = [run for run in claims if run is not None]

    assert sorted(run.namespace for run in claimed) == ["a", "b"]
    assert len({run.id for run in claimed}) == 2
    assert all(run.status == OptimizationRunStatus.RUNNING for run in claimed)
    assert all(run.started_at == T0 for run in claimed)


async def test_claim_order_is_fifo(backend):
    repo = backend.runs()
    late, _ = await _enqueue(repo, namespace="late", at=T0 + timedelta(minutes=1))
    early, _ = await _enqueue(repo, namespace="early", at=T0)

    first = await repo.claim_next(claimed_by="c", started_at=T0)
    second = await repo.claim_next(claimed_by="c", started_at=T0)

    assert [first.id, second.id] == [early.id, late.id]
    assert await repo.claim_next(claimed_by="c", started_at=T0) is None


async def test_enqueue_if_due_creates_one_scheduled_run_per_interval(backend):
    interval = timedelta(hours=1)
    repos = [backend.runs(0), backend.runs(1)]

    created = await asyncio.gather(
        *(repo.enqueue_if_due(requested_at=T0, due_before=T0 - interval, requested_by="c") for repo in repos * 3)
    )
    scheduled = [run for run in created if run is not None]
    assert len(scheduled) == 1
    assert scheduled[0].trigger == OptimizationRunTrigger.SCHEDULED
    assert scheduled[0].namespace is None

    run = await repos[0].claim_next(claimed_by="c", started_at=T0)
    assert run is not None
    # Still running: never schedule a second cluster-wide scan.
    later = T0 + timedelta(hours=2)
    assert await repos[1].enqueue_if_due(requested_at=later, due_before=later - interval) is None
    await repos[0].finish(
        run.id,
        OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=T0 + timedelta(minutes=1)),
    )

    within = T0 + timedelta(minutes=30)
    assert await repos[1].enqueue_if_due(requested_at=within, due_before=within - interval) is None
    after = T0 + timedelta(hours=1, seconds=1)
    assert await repos[1].enqueue_if_due(requested_at=after, due_before=after - interval) is not None


async def test_recent_manual_cluster_run_satisfies_the_schedule(backend):
    repo = backend.runs()
    await _enqueue(repo, at=T0)
    now = T0 + timedelta(minutes=5)

    assert await repo.enqueue_if_due(requested_at=now, due_before=now - timedelta(hours=1)) is None


async def test_namespaced_runs_do_not_satisfy_the_cluster_schedule(backend):
    repo = backend.runs()
    await _enqueue(repo, namespace="prod", at=T0)
    now = T0 + timedelta(minutes=5)

    assert await repo.enqueue_if_due(requested_at=now, due_before=now - timedelta(hours=1)) is not None


async def test_fail_abandoned_only_fails_old_running_runs(backend):
    repo = backend.runs()
    await _enqueue(repo, namespace="old", at=T0)
    await _enqueue(repo, namespace="fresh", at=T0)
    old = await repo.claim_next(claimed_by="dead", started_at=T0)
    fresh = await repo.claim_next(claimed_by="alive", started_at=T0 + timedelta(minutes=50))
    queued, _ = await _enqueue(repo, namespace="waiting", at=T0)

    count = await repo.fail_abandoned(
        started_before=T0 + timedelta(minutes=30), completed_at=T0 + timedelta(hours=1), error="abandoned"
    )

    assert count == 1
    assert (await repo.get(old.id)).status == OptimizationRunStatus.FAILED
    assert (await repo.get(old.id)).error == "abandoned"
    assert (await repo.get(fresh.id)).status == OptimizationRunStatus.RUNNING
    assert (await repo.get(queued.id)).status == OptimizationRunStatus.QUEUED


async def test_finish_does_not_overwrite_a_reaped_run(backend):
    repo = backend.runs()
    await _enqueue(repo)
    run = await repo.claim_next(claimed_by="slow", started_at=T0)
    await backend.runs(1).fail_abandoned(
        started_before=T0 + timedelta(minutes=30), completed_at=T0 + timedelta(minutes=31), error="abandoned"
    )

    late = await repo.finish(
        run.id,
        OptimizationRunCompletion(
            status=OptimizationRunStatus.SUCCEEDED, completed_at=T0 + timedelta(minutes=40), recommendation_count=7
        ),
    )

    assert late.status == OptimizationRunStatus.FAILED
    assert late.error == "abandoned"
    assert late.recommendation_count == 0
    assert late == await repo.get(run.id)


async def test_finish_unknown_run_raises(backend):
    with pytest.raises(ValueError):
        await backend.runs().finish(
            999_999, OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=T0)
        )


async def test_enqueue_if_due_schedules_once_while_runs_are_claimed_concurrently(backend):
    interval = timedelta(hours=1)
    controllers = [backend.runs(0), backend.runs(1)]

    for _ in range(10):
        await asyncio.gather(
            *(repo.enqueue_if_due(requested_at=T0, due_before=T0 - interval, requested_by="c") for repo in controllers),
            controllers[0].claim_next(claimed_by="c0", started_at=T0),
            controllers[1].claim_next(claimed_by="c1", started_at=T0),
        )

    assert len(await controllers[0].list_runs(limit=50)) == 1


async def test_find_recent_covering_matches_pending_and_recently_finished_runs(backend):
    repo = backend.runs()
    window_start = T0 + timedelta(minutes=10)
    assert await repo.find_recent_covering(namespace="prod", completed_after=window_start) is None

    staging, _ = await _enqueue(repo, namespace="staging", at=T0)
    assert await repo.find_recent_covering(namespace="prod", completed_after=window_start) is None
    assert (await repo.find_recent_covering(namespace="staging", completed_after=window_start)).id == staging.id
    assert await repo.find_recent_covering(namespace=None, completed_after=window_start) is None

    cluster, _ = await _enqueue(repo, at=T0 + timedelta(minutes=1))
    claimed = await repo.claim_next(claimed_by="c", started_at=T0 + timedelta(minutes=2))
    assert claimed is not None and claimed.id == staging.id
    claimed = await repo.claim_next(claimed_by="c", started_at=T0 + timedelta(minutes=2))
    assert claimed is not None and claimed.id == cluster.id
    # A running cluster-wide run covers every namespace.
    assert (await repo.find_recent_covering(namespace="prod", completed_after=window_start)).id == cluster.id
    assert (await repo.find_recent_covering(namespace=None, completed_after=window_start)).id == cluster.id

    await repo.finish(
        cluster.id,
        OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=T0 + timedelta(minutes=12)),
    )
    assert (await repo.find_recent_covering(namespace="prod", completed_after=window_start)).id == cluster.id
    later = T0 + timedelta(minutes=13)
    assert await repo.find_recent_covering(namespace=None, completed_after=later) is None
    # The namespaced run is still running.
    assert (await repo.find_recent_covering(namespace="staging", completed_after=later)).id == staging.id


async def test_prune_finished_deletes_only_old_terminal_runs(backend):
    repo = backend.runs()
    await _enqueue(repo, namespace="old", at=T0)
    await _enqueue(repo, namespace="recent", at=T0)
    await _enqueue(repo, namespace="running", at=T0)
    old = await repo.claim_next(claimed_by="c", started_at=T0)
    recent = await repo.claim_next(claimed_by="c", started_at=T0)
    running = await repo.claim_next(claimed_by="c", started_at=T0)
    queued, _ = await _enqueue(repo, namespace="queued", at=T0)
    await repo.finish(
        old.id, OptimizationRunCompletion(status=OptimizationRunStatus.FAILED, completed_at=T0 + timedelta(days=1))
    )
    await repo.finish(
        recent.id,
        OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=T0 + timedelta(days=20)),
    )

    pruned = await repo.prune_finished(completed_before=T0 + timedelta(days=10))

    assert pruned == 1
    assert await repo.get(old.id) is None
    assert {run.id for run in await repo.list_runs(limit=10)} == {recent.id, running.id, queued.id}


# --------------------------------------------------------------------------- #
# Run outcome (E10)                                                            #
# --------------------------------------------------------------------------- #


async def test_finish_round_trips_counts_source_health_and_diff(backend):
    repo = backend.runs()
    queued, _ = await _enqueue(repo)
    run = await repo.claim_next(claimed_by="c", started_at=T0)
    outcomes = [
        SourceRunOutcome(source="greenkube", status=SourceRunStatus.SUCCEEDED, complete=True, recommendation_count=4),
        SourceRunOutcome(source="vpa", status=SourceRunStatus.FAILED, error="timeout"),
    ]

    finished = await repo.finish(
        run.id,
        OptimizationRunCompletion(
            status=OptimizationRunStatus.SUCCEEDED,
            completed_at=T0 + timedelta(minutes=2),
            recommendation_count=4,
            analyzer_count=2,
            failed_analyzer_count=1,
            new_count=2,
            changed_count=1,
            resolved_count=3,
            unchanged_count=1,
            source_outcomes=outcomes,
            diff=OptimizationRunDiff(new=["fp-a", "fp-b"], changed=["fp-c"], resolved=["x", "y", "z"]),
            error="Sources failed or incomplete: vpa",
        ),
    )

    stored = await repo.get(queued.id)
    assert finished == stored
    assert stored.status == OptimizationRunStatus.SUCCEEDED
    assert stored.completed_at == T0 + timedelta(minutes=2)
    assert (stored.new_count, stored.changed_count, stored.resolved_count, stored.unchanged_count) == (2, 1, 3, 1)
    assert stored.source_outcomes == outcomes
    assert stored.diff == OptimizationRunDiff(new=["fp-a", "fp-b"], changed=["fp-c"], resolved=["x", "y", "z"])
    assert stored.claimed_by == "c"


async def test_list_runs_returns_newest_first_with_limit(backend):
    repo = backend.runs()
    ids = []
    for minute, namespace in enumerate(["a", "b", "c"]):
        run, _ = await _enqueue(repo, namespace=namespace, at=T0 + timedelta(minutes=minute))
        ids.append(run.id)

    runs = await repo.list_runs(limit=2)

    assert [run.id for run in runs] == [ids[2], ids[1]]
    assert (await repo.latest()).id == ids[2]


# --------------------------------------------------------------------------- #
# Migration 0045 upgrade from existing data                                    #
# --------------------------------------------------------------------------- #


async def _apply_until(connection, db_type: str, target_version: int) -> None:
    runner = MigrationRunner(db_type)
    await runner._ensure_migrations_table(connection)
    applied = await runner._get_applied_versions(connection)
    for version, name, sql in runner._discover_scripts():
        if version > target_version:
            break
        if version in applied:
            continue
        await runner._execute_sql(connection, sql)
        await runner._record_migration(connection, version, name)


async def test_migration_0045_keeps_existing_runs_sqlite(tmp_path):
    path = str(tmp_path / "upgrade.sqlite")
    async with aiosqlite.connect(path) as connection:
        await _apply_until(connection, "sqlite", QUEUE_MIGRATION - 1)
        await connection.execute(
            """INSERT INTO optimization_runs (id, status, namespace, started_at, completed_at,
            recommendation_count, analyzer_count, failed_analyzer_count, error)
            VALUES (7, 'succeeded', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 12, 3, 0, NULL),
                   (9, 'running', 'prod', '2026-01-02T00:00:00Z', NULL, 0, 3, 0, NULL)"""
        )
        await connection.commit()
        await MigrationRunner("sqlite").run(connection)
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute("SELECT * FROM optimization_runs ORDER BY id")
        rows = [dict(row) for row in await cursor.fetchall()]
        cursor = await connection.execute(
            "INSERT INTO optimization_runs (status, requested_at) VALUES ('queued', '2026-01-03T00:00:00Z')"
        )
        new_id = cursor.lastrowid
        await connection.commit()

    assert [(row["id"], row["status"], row["trigger_type"]) for row in rows] == [
        (7, "succeeded", "legacy"),
        (9, "running", "legacy"),
    ]
    assert rows[0]["requested_at"] == "2026-01-01T00:00:00Z"
    assert rows[0]["recommendation_count"] == 12
    assert new_id == 10


async def test_migration_0045_keeps_existing_runs_postgres():
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL migration tests.")
    schema = f"gk_upgrade_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(f"SET search_path TO {schema}")
        await _apply_until(connection, "postgres", QUEUE_MIGRATION - 1)
        await connection.execute(
            """INSERT INTO optimization_runs (status, namespace, started_at, completed_at,
            recommendation_count, analyzer_count, failed_analyzer_count)
            VALUES ('succeeded', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 12, 3, 0)"""
        )
        await MigrationRunner("postgres").run(connection)
        row = await connection.fetchrow("SELECT * FROM optimization_runs")
        queued = await connection.fetchrow(
            "INSERT INTO optimization_runs (status) VALUES ('queued') RETURNING started_at, requested_at, trigger_type"
        )
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()

    assert row["trigger_type"] == "legacy"
    assert row["requested_at"] == row["started_at"]
    assert row["recommendation_count"] == 12
    assert queued["started_at"] is None
    assert queued["requested_at"] is not None
    assert queued["trigger_type"] == "direct"
