"""collection_windows records on SQLite and PostgreSQL (COL-04)."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_collection_window_repository import (
    REASON_BEYOND_BACKFILL,
    REASON_NO_SCRAPE_DATA,
    STATUS_COLLECTED,
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
    create_collection_window_repository,
)
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
SOURCE = "combined_metrics"


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[tuple[str, DatabaseManager], None]:
    backend = request.param
    dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL collection window tests.")
        schema = f"gk_cw_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "windows.db"))
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


def _record(start: datetime, status: str, *, steps: int = 1, reason: str | None = None, cluster: str = "c1"):
    return CollectionWindowRecord(
        cluster_uid=cluster,
        source=SOURCE,
        window_start=start,
        window_end=start + steps * STEP,
        status=status,
        expected_steps=steps,
        steps_with_data=1 if status == STATUS_COLLECTED else 0,
        pod_rows=3 if status == STATUS_COLLECTED else 0,
        reason=reason,
        opencost_total_cost=1.5 if status == STATUS_COLLECTED else None,
    )


@pytest.mark.asyncio
async def test_records_are_upserted_listed_retried_and_expired(database):
    backend, manager = database
    repo = create_collection_window_repository(manager, backend)

    assert not await repo.has_records("c1", SOURCE)
    await repo.record(
        [
            _record(BASE - 12 * STEP, STATUS_GAP, steps=12, reason=REASON_BEYOND_BACKFILL),
            _record(BASE, STATUS_FAILED, reason=REASON_NO_SCRAPE_DATA),
            _record(BASE + STEP, STATUS_FAILED, reason=REASON_NO_SCRAPE_DATA),
            _record(BASE + 2 * STEP, STATUS_COLLECTED),
            _record(BASE, STATUS_COLLECTED, cluster="c2"),
        ]
    )
    assert await repo.has_records("c1", SOURCE)

    overlapping = await repo.list_overlapping("c1", SOURCE, BASE - STEP, BASE + 3 * STEP)
    assert [(r.window_start, r.status, r.expected_steps) for r in overlapping] == [
        (BASE - 12 * STEP, STATUS_GAP, 12),
        (BASE, STATUS_FAILED, 1),
        (BASE + STEP, STATUS_FAILED, 1),
        (BASE + 2 * STEP, STATUS_COLLECTED, 1),
    ]
    assert overlapping[-1].pod_rows == 3 and overlapping[-1].opencost_total_cost == pytest.approx(1.5)
    assert overlapping[-1].collected_at is not None

    failed = await repo.list_failed("c1", SOURCE, BASE, limit=10)
    assert [r.window_start for r in failed] == [BASE, BASE + STEP]

    # A retried step with data replaces its failed record.
    await repo.record([_record(BASE + STEP, STATUS_COLLECTED)])
    hole = await repo.last_hole("c1", SOURCE)
    assert hole is not None and hole.window_start == BASE and hole.status == STATUS_FAILED

    assert await repo.expire_failed("c1", SOURCE, BASE + STEP) == 1
    hole = await repo.last_hole("c1", SOURCE)
    assert hole is not None and hole.status == STATUS_GAP and hole.reason == REASON_NO_SCRAPE_DATA
    assert await repo.list_failed("c1", SOURCE, BASE - 24 * STEP, limit=10) == []


@pytest.mark.asyncio
async def test_a_failed_recollection_never_downgrades_a_collected_step(database):
    """Review 3: --last beyond Prometheus retention finds no data for a step whose rows are stored."""
    backend, manager = database
    repo = create_collection_window_repository(manager, backend)
    await repo.record([_record(BASE, STATUS_COLLECTED)])

    await repo.record([_record(BASE, STATUS_FAILED, reason=REASON_NO_SCRAPE_DATA)])

    (record,) = await repo.list_overlapping("c1", SOURCE, BASE, BASE + STEP)
    assert record.status == STATUS_COLLECTED and record.pod_rows == 3
    assert await repo.last_hole("c1", SOURCE) is None


@pytest.mark.asyncio
async def test_stored_metric_timestamps_are_distinct(database):
    backend, manager = database
    repo = create_collection_window_repository(manager, backend)
    metrics = (
        SQLiteCombinedMetricsRepository(manager) if backend == "sqlite" else PostgresCombinedMetricsRepository(manager)
    )
    rows = [
        CombinedMetric(pod_name=pod, namespace="ns", timestamp=BASE + k * STEP, duration_seconds=300)
        for pod in ("a", "b")
        for k in range(2)
    ]
    await metrics.write_combined_metrics(rows)

    assert await repo.stored_metric_timestamps() == [BASE, BASE + STEP]
    assert await repo.stored_metric_timestamps(since=BASE + STEP) == [BASE + STEP]


@pytest.mark.asyncio
async def test_pod_rows_store_the_memory_peak_and_hourly_rows_keep_it(database):
    """COL-12 review: the pod step peak is stored and compressed with MAX(peak)."""
    from greenkube.core.metrics_compressor import TABLES

    backend, manager = database
    metrics = (
        SQLiteCombinedMetricsRepository(manager) if backend == "sqlite" else PostgresCombinedMetricsRepository(manager)
    )
    rows = [
        CombinedMetric(
            pod_name="a",
            namespace="ns",
            timestamp=BASE + k * STEP,
            duration_seconds=300,
            memory_usage_bytes=240,
            memory_usage_max_bytes=400 if k == 1 else None,
        )
        for k in range(3)
    ]
    await metrics.write_combined_metrics(rows)

    stored = sorted(await metrics.read_combined_metrics(BASE, BASE + 3 * STEP), key=lambda r: r.timestamp)
    assert [r.memory_usage_max_bytes for r in stored] == [None, 400, None]

    spec = next(table for table in TABLES if table.raw == "combined_metrics")
    assert "MAX(COALESCE(memory_usage_max_bytes, memory_usage_bytes))" in spec.compress_sql(backend)


@pytest.mark.asyncio
async def test_latest_container_usage_sources_reads_the_newest_step(database):
    """COL-05: /health says where container usage comes from (cri-dockerd: Metrics API point samples)."""
    backend, manager = database
    repo = create_collection_window_repository(manager, backend)
    metrics = (
        SQLiteCombinedMetricsRepository(manager) if backend == "sqlite" else PostgresCombinedMetricsRepository(manager)
    )
    assert await repo.latest_container_usage_sources() == set()
    rows = [
        CombinedMetric(
            pod_name="a",
            namespace="ns",
            container_name="app",
            timestamp=BASE + k * STEP,
            duration_seconds=300,
            usage_source=source,
        )
        for k, source in ((0, "prometheus"), (1, "metrics_server"))
    ]
    await metrics.write_container_metrics(rows)

    assert await repo.latest_container_usage_sources() == {"metrics_server"}


@pytest.mark.asyncio
async def test_cost_pending_status_round_trips_and_is_listed(database):
    """COST-01: a step whose cost waits for OpenCost is stored, listed, then cleared by its re-pricing."""
    from dataclasses import replace

    from greenkube.storage.base_collection_window_repository import COST_STATUS_PENDING

    backend, manager = database
    repository = create_collection_window_repository(manager, backend)
    pending = replace(_record(BASE, STATUS_COLLECTED), cost_status=COST_STATUS_PENDING)
    await repository.record([pending, _record(BASE + STEP, STATUS_COLLECTED)])

    listed = await repository.list_cost_pending("c1", SOURCE, BASE, BASE + 4 * STEP, 10)
    assert [record.window_start for record in listed] == [BASE]
    assert listed[0].cost_status == COST_STATUS_PENDING

    await repository.record([_record(BASE, STATUS_COLLECTED)])
    assert await repository.list_cost_pending("c1", SOURCE, BASE, BASE + 4 * STEP, 10) == []
