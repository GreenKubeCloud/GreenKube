"""Collection watermarks (A2) and half-open range reads (BUG-03) on SQLite and PostgreSQL."""

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
from greenkube.storage.base_collection_watermark_repository import create_collection_watermark_repository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

# Recent enough that summaries read raw rows (older data is served from hourly rollups).
BASE = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
STEP = timedelta(minutes=5)


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[tuple[str, DatabaseManager], None]:
    backend = request.param
    dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL watermark tests.")
        schema = f"gk_wm_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "watermarks.db"))
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


@pytest.mark.asyncio
async def test_watermark_is_created_advanced_and_never_moves_backwards(database):
    backend, manager = database
    repo = create_collection_watermark_repository(manager, backend)

    assert await repo.get("cluster-a", "combined_metrics") is None

    first = await repo.advance("cluster-a", "combined_metrics", BASE)
    later = await repo.advance("cluster-a", "combined_metrics", BASE + STEP)
    stale = await repo.advance("cluster-a", "combined_metrics", BASE)
    other = await repo.advance("cluster-b", "combined_metrics", BASE - STEP)

    assert first.last_complete_end == BASE
    assert later.last_complete_end == BASE + STEP
    assert stale.last_complete_end == BASE + STEP
    assert stale.updated_at.tzinfo is not None
    stored = await repo.get("cluster-a", "combined_metrics")
    assert stored is not None and stored.last_complete_end == BASE + STEP
    assert other.last_complete_end == BASE - STEP
    assert [(w.cluster_uid, w.last_complete_end) for w in await repo.list_all()] == [
        ("cluster-a", BASE + STEP),
        ("cluster-b", BASE - STEP),
    ]
    assert [w.cluster_uid for w in await repo.list_all("cluster-b")] == ["cluster-b"]


@pytest.mark.asyncio
async def test_watermark_keeps_sub_second_precision_ordering(database):
    backend, manager = database
    repo = create_collection_watermark_repository(manager, backend)

    await repo.advance("c", "s", BASE + timedelta(seconds=1))
    result = await repo.advance("c", "s", BASE + timedelta(microseconds=500_000))

    assert result.last_complete_end == BASE + timedelta(seconds=1)


def _metric(pod: str, timestamp: datetime, container: str | None = None) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="ns",
        container_name=container,
        timestamp=timestamp,
        duration_seconds=300,
        joules=1.0,
        total_cost=0.01,
    )


@pytest.mark.asyncio
async def test_range_reads_are_half_open(database):
    """A row stamped exactly at ``end`` belongs to the next window, never to both."""
    backend, manager = database
    repo = (
        PostgresCombinedMetricsRepository(manager)
        if backend == "postgres"
        else SQLiteCombinedMetricsRepository(manager)
    )
    await repo.write_combined_metrics(
        [
            _metric("p", BASE),
            _metric("p", BASE + STEP),
            _metric("p", BASE, container="app"),
            _metric("p", BASE + STEP, container="app"),
        ]
    )

    first = await repo.read_combined_metrics(BASE, BASE + STEP)
    second = await repo.read_combined_metrics(BASE + STEP, BASE + 2 * STEP)
    containers = await repo.read_container_metrics(BASE, BASE + STEP)
    total, page, _cursor = await repo.read_combined_metrics_cursor(BASE, BASE + STEP)
    summary = await repo.aggregate_summary(BASE, BASE + STEP)

    assert [m.timestamp for m in first if not m.container_name] == [BASE]
    assert [m.timestamp for m in second if not m.container_name] == [BASE + STEP]
    assert [m.timestamp for m in containers] == [BASE]
    assert total == 1 and [m.timestamp for m in page] == [BASE]
    assert summary["row_count"] == 1
