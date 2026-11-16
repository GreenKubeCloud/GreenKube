"""The node idle share is kept, rolled up and shown next to the allocated totals (NRG-02, ROL-07, API-04).

One node, one pod reserving a quarter of it for one hour: the summary returns
the pod energy as allocated, the ``__idle__`` energy as idle and their sum as
the cluster total, before and after the hour is compressed and its raw rows
pruned. A namespace-filtered summary has no idle share.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from greenkube.api.routers.metrics import metrics_summary, metrics_timeseries
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

NOW = datetime.now(timezone.utc)
HOUR = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
POD_JOULES = 150.0 * 300  # per 5-minute step
IDLE_JOULES = 450.0 * 300


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "1")
    monkeypatch.setenv("METRICS_AGGREGATED_RETENTION_DAYS", "-1")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL idle tests.")
        schema = f"gk_idleshare_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "idle.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        config.reload()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _repository(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresCombinedMetricsRepository(manager)
    return SQLiteCombinedMetricsRepository(manager)


async def _count(manager: DatabaseManager, table: str) -> int:
    async with manager.connection_scope() as connection:
        conn: Any = connection
        if manager.db_type == "postgres":
            return int(await conn.fetchval(f"SELECT COUNT(*) FROM {table}"))
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            return int((await cursor.fetchone())[0])


async def _seed(repo) -> None:
    steps = [HOUR + timedelta(minutes=5 * i) for i in range(12)]
    await repo.write_combined_metrics(
        [
            CombinedMetric(
                pod_name="api",
                namespace="prod",
                total_cost=0.01,
                co2e_grams=1.0,
                embodied_co2e_grams=0.1,
                joules=POD_JOULES,
                cpu_request=500,
                timestamp=step,
                duration_seconds=300,
                node="node-a",
            )
            for step in steps
        ]
    )
    await repo.write_idle_metrics(
        [
            NodeIdleMetric(
                node="node-a",
                timestamp=step,
                duration_seconds=300,
                joules=IDLE_JOULES,
                co2e_grams=3.0,
                embodied_co2e_grams=0.3,
                total_cost=0.03,
                cost_cpu=0.02,
                cost_ram=0.01,
                cost_source="modeled",
                cpu_unreserved_cores=1.5,
                memory_unreserved_bytes=2e9,
            )
            for step in steps
        ]
    )


async def _summary(repo, namespace=None):
    return await metrics_summary(namespace=namespace, last="2d", repo=repo)


async def test_summary_shows_allocated_idle_and_cluster_totals_before_and_after_compression(db):
    repo = _repository(db)
    await _seed(repo)

    before = await _summary(repo)
    assert before.total_energy_joules == pytest.approx(12 * POD_JOULES)
    assert before.idle_energy_joules == pytest.approx(12 * IDLE_JOULES)
    # Node power × covered seconds: (150 W + 450 W) × 3600 s.
    assert before.cluster_total_energy_joules == pytest.approx(600.0 * 3600, rel=1e-6)
    assert before.cluster_total_co2e_grams == pytest.approx(48.0)
    assert before.cluster_total_cost == pytest.approx(0.48)

    await MetricsCompressor(db, Config(), collection_watermark_provider=None, rollup_enabled=False).run(now=NOW)
    assert await _count(db, "node_idle_metrics") == 0, "idle raw rows are pruned with the raw retention"
    assert await _count(db, "node_idle_metrics_hourly") == 1

    after = await _summary(repo)
    for field in ("total_energy_joules", "idle_energy_joules", "cluster_total_energy_joules", "idle_cost"):
        assert getattr(after, field) == pytest.approx(getattr(before, field), rel=1e-9), field

    series = await metrics_timeseries(namespace=None, last="2d", granularity="day", repo=repo)
    assert sum(point.idle_energy_joules or 0.0 for point in series) == pytest.approx(12 * IDLE_JOULES)


async def test_a_namespace_filtered_summary_has_no_idle_share(db):
    repo = _repository(db)
    await _seed(repo)
    filtered = await _summary(repo, namespace="prod")
    assert filtered.total_energy_joules == pytest.approx(12 * POD_JOULES)
    assert filtered.idle_energy_joules is None
    assert filtered.cluster_total_energy_joules is None
