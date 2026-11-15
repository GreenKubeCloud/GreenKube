"""Write semantics of the metric repositories (WP-02): batched lookups, atomic windows, no swallowed rows."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.exceptions import QueryError
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.collection_watermark_repository import PostgresCollectionWatermarkRepository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

BASE = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[tuple[str, DatabaseManager], None]:
    backend = request.param
    dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL write tests.")
        schema = f"gk_write_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "write.db"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield backend, manager
    finally:
        await manager.close()
        if dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repo(backend: str, manager: DatabaseManager):
    if backend == "sqlite":
        return SQLiteCombinedMetricsRepository(manager)
    return PostgresCombinedMetricsRepository(manager)


def _row(pod: str = "p", **overrides) -> CombinedMetric:
    values = {
        "pod_name": pod,
        "namespace": "ns",
        "timestamp": BASE,
        "duration_seconds": 300,
        "cpu_request": 500,
        "memory_request": 1024,
        "owner_kind": "Deployment",
        "owner_name": "d",
        "joules": 10.0,
        "metadata_source": "k8s_snapshot",
        "node_source": "snapshot",
    }
    values.update(overrides)
    return CombinedMetric(**values)


async def test_pods_without_container_rows_are_checked_in_one_query_on_sqlite(tmp_path, monkeypatch):
    """Follow-up review: a cri-dockerd backfill (no container row at all) must not issue one SELECT per pod."""
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "batch.db"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repo = SQLiteCombinedMetricsRepository(manager)
        await repo.write_combined_metrics([_row("p0"), _row("p0", container_name="app", joules=10.0)])
        statements: list[str] = []
        assert manager.connection is not None
        await manager.connection.set_trace_callback(statements.append)
        rows = [_row(f"p{index}", joules=20.0) for index in range(40)]
        await repo.write_combined_metrics(rows, replace_containers=True)
        await manager.connection.set_trace_callback(lambda _statement: None)

        lookups = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "container_metrics" in s]
        assert len(lookups) == 1
        stored = {row.pod_name: row.joules for row in await repo.read_combined_metrics(BASE, BASE + timedelta(1))}
        # p0 has stored container rows and none in the batch: its stored pod row is kept.
        assert stored["p0"] == 10.0
        assert stored["p39"] == 20.0 and len(stored) == 40
    finally:
        await manager.close()


def _idle(cost: float, source: str, joules: float = 50.0) -> NodeIdleMetric:
    return NodeIdleMetric(
        node="n1",
        timestamp=BASE,
        duration_seconds=300,
        joules=joules,
        co2e_grams=1.0,
        embodied_co2e_grams=0.5,
        total_cost=cost,
        cost_cpu=cost / 2,
        cost_ram=cost / 2,
        cost_source=source,
        cpu_unreserved_cores=1.0,
        memory_unreserved_bytes=1.0,
    )


async def test_recollection_never_replaces_a_cost_with_a_worse_source_and_a_better_one_wins(database):
    """STO-01 a) and b): OpenCost down on the second pass keeps the stored OpenCost cost; OpenCost back replaces it."""
    backend, manager = database
    repo = _repo(backend, manager)
    first = {"total_cost": 1.0, "cost_cpu": 0.6, "cost_ram": 0.4, "cost_source": "opencost"}
    await repo.write_combined_metrics([_row(**first), _row(container_name="app", **first)])
    await repo.write_idle_metrics([_idle(2.0, "opencost")])

    degraded = {"total_cost": 0.3, "cost_cpu": 0.2, "cost_ram": 0.1, "cost_source": "custom_rate"}
    await repo.write_combined_metrics(
        [_row(joules=12.0, **degraded), _row(container_name="app", joules=12.0, **degraded)],
        replace_containers=True,
    )
    await repo.write_idle_metrics([_idle(0.7, "custom_rate", joules=55.0)])

    window = (BASE, BASE + timedelta(minutes=5))
    (pod,) = await repo.read_combined_metrics(*window)
    (container,) = await repo.read_container_metrics(*window)
    (idle,) = await repo.read_idle_metrics(*window)
    for row in (pod, container):
        assert (row.total_cost, row.cost_cpu, row.cost_ram, row.cost_source) == (1.0, 0.6, 0.4, "opencost")
        assert row.joules == 12.0 and row.cpu_request == 500 and row.owner_name == "d"
    assert (idle.total_cost, idle.cost_source, idle.joules) == (2.0, "opencost", 55.0)

    better = {"total_cost": 1.5, "cost_cpu": 1.0, "cost_ram": 0.5, "cost_source": "opencost"}
    await repo.write_combined_metrics([_row(**better), _row(container_name="app", **better)], replace_containers=True)
    (pod,) = await repo.read_combined_metrics(*window)
    (container,) = await repo.read_container_metrics(*window)
    assert (pod.total_cost, pod.cost_source) == (1.5, "opencost")
    assert (container.total_cost, container.cost_cpu) == (1.5, 1.0)


async def test_window_write_removes_rows_the_new_pass_no_longer_produces(database):
    """STO-01: a re-written window keeps one generation; steps without rows in the pass keep their rows."""
    backend, manager = database
    repo = _repo(backend, manager)
    later = BASE + timedelta(minutes=5)
    window = (BASE, BASE + timedelta(minutes=10))
    first = [
        _row("p"),
        _row("p", container_name="app"),
        _row("q"),
        _row("q", container_name="app"),
        _row("q", timestamp=later),
    ]
    await repo.write_combined_metrics(first, replace_containers=True, window=window, run_id="r1")
    await repo.write_idle_metrics(
        [_idle(1.0, "opencost"), replace(_idle(1.0, "opencost"), node="n2")], window=window, run_id="r1"
    )

    # The second pass has only p at BASE (q is gone) and nothing at the later step.
    await repo.write_combined_metrics(
        [_row("p", joules=11.0), _row("p", container_name="app", joules=11.0)],
        replace_containers=True,
        window=window,
        run_id="r2",
    )
    await repo.write_idle_metrics([_idle(1.0, "opencost", joules=60.0)], window=window, run_id="r2")

    pods = await repo.read_combined_metrics(*window)
    assert sorted((r.pod_name, r.timestamp, r.joules) for r in pods) == [("p", BASE, 11.0), ("q", later, 10.0)]
    containers = await repo.read_container_metrics(*window)
    assert [(c.pod_name, c.joules) for c in containers] == [("p", 11.0)]
    idle = await repo.read_idle_metrics(*window)
    assert [(i.node, i.joules) for i in idle] == [("n1", 60.0)]
    async with manager.connection_scope() as conn:
        query = "SELECT DISTINCT collection_run_id FROM {} WHERE pod_name = 'p'"
        for table in ("combined_metrics", "container_metrics"):
            if backend == "postgres":
                runs = [row[0] for row in await conn.fetch(query.format(table))]
            else:
                async with conn.execute(query.format(table)) as cursor:
                    runs = [row[0] for row in await cursor.fetchall()]
            assert runs == ["r2"]


def _watermarks(backend: str, manager: DatabaseManager):
    if backend == "sqlite":
        return SQLiteCollectionWatermarkRepository(manager)
    return PostgresCollectionWatermarkRepository(manager)


async def test_a_window_is_written_atomically_with_its_watermark(database):
    """STO-03: pod, container and idle rows and the watermark commit together or not at all."""
    backend, manager = database
    repo = _repo(backend, manager)
    watermarks = _watermarks(backend, manager)
    window = (BASE, BASE + timedelta(minutes=5))
    rows = [_row("p"), _row("p", container_name="app")]

    async def failing_commit(_conn):
        raise RuntimeError("watermark store unavailable")

    with pytest.raises(RuntimeError):
        await repo.write_window(rows, [_idle(1.0, "opencost")], window=window, run_id="r1", on_commit=failing_commit)
    bad_idle = replace(_idle(1.0, "opencost"), duration_seconds=2**64)
    with pytest.raises(QueryError):
        await repo.write_window(rows, [bad_idle], window=window, run_id="r1")
    assert await repo.read_combined_metrics(*window) == []
    assert await repo.read_container_metrics(*window) == []
    assert await repo.read_idle_metrics(*window) == []
    assert await watermarks.get("c", "combined_metrics") is None

    async def advance(conn):
        await watermarks.advance("c", "combined_metrics", window[1], connection=conn)

    written = await repo.write_window(rows, [_idle(1.0, "opencost")], window=window, run_id="r2", on_commit=advance)
    assert written == 2
    assert len(await repo.read_combined_metrics(*window)) == 1
    assert len(await repo.read_container_metrics(*window)) == 1
    assert len(await repo.read_idle_metrics(*window)) == 1
    stored = await watermarks.get("c", "combined_metrics")
    assert stored is not None and stored.last_complete_end == window[1]


async def test_sqlite_and_postgres_writers_reject_the_whole_batch_on_a_bad_row(database):
    """STO-04 (SIM A1): no row of the batch is stored and QueryError is raised."""
    backend, manager = database
    repo = _repo(backend, manager)
    with pytest.raises(QueryError):
        await repo.write_combined_metrics([_row("a"), _row("b", restart_count=2**64), _row("c")])
    assert await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5)) == []
    assert await repo.write_combined_metrics([_row("a"), _row("b"), _row("c")]) == 3
