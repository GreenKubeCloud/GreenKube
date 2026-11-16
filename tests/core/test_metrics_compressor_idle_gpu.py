"""Retention of ``__idle__`` rows and GPU power in hourly tables (B2, B5 with A3/E3).

* ``node_idle_metrics`` had no retention: its raw per-step rows grew forever.
  They are now pruned with the pod rows of ``combined_metrics`` (same raw
  retention, same compression watermark).
* The compressed hourly tables had no ``gpu_power_watts`` (migration 0060 added
  GPU energy to the workload usage rollup only), so an hour re-rolled from the
  hourly tables lost its GPU energy. Migration 0064 adds it.

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime as real_datetime
from datetime import timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

import greenkube.core.metrics_compressor as compressor_module
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.core.workload_usage import rollup_rows
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

#: Fixed origin of the synthetic history; the compressor's clock is frozen relative to it.
T0 = real_datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
DAYS = 9


class _FrozenClock:
    def __init__(self) -> None:
        self.now = T0


@pytest.fixture
def clock(monkeypatch) -> _FrozenClock:
    frozen = _FrozenClock()

    class FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen.now

    monkeypatch.setattr(compressor_module, "datetime", FakeDateTime)
    return frozen


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def metrics_db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "7")
    monkeypatch.setenv("METRICS_AGGREGATED_RETENTION_DAYS", "-1")
    monkeypatch.setenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL compression tests.")
        schema = f"gk_idle_gpu_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "idle_gpu.sqlite"))
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


def _repository(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresCombinedMetricsRepository(manager)
    return SQLiteCombinedMetricsRepository(manager)


async def _fetch(manager: DatabaseManager, query: str) -> list[tuple]:
    async with manager.connection_scope() as connection:
        conn: Any = connection
        if manager.db_type == "postgres":
            return [tuple(row) for row in await conn.fetch(query)]
        async with conn.execute(query) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


def _timestamp(value: Any) -> real_datetime:
    parsed = value if isinstance(value, real_datetime) else real_datetime.fromisoformat(str(value))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sample(index: int, *, container: str | None = None, gpu: float | None = None) -> CombinedMetric:
    return CombinedMetric(
        pod_name="trainer-0",
        namespace="ml",
        container_name=container,
        owner_kind="StatefulSet",
        owner_name="trainer",
        node="gpu-1",
        timestamp=T0 + timedelta(minutes=5 * index),
        total_cost=1.0,
        co2e_grams=1.0,
        joules=1.0,
        pue=1.0,
        grid_intensity=1.0,
        cpu_request=1000,
        memory_request=1024**3,
        duration_seconds=300,
        gpu_power_watts=gpu,
    )


def _idle(index: int) -> NodeIdleMetric:
    return NodeIdleMetric(
        node="gpu-1",
        timestamp=T0 + timedelta(minutes=5 * index),
        duration_seconds=300,
        joules=1.0,
        co2e_grams=1.0,
        embodied_co2e_grams=1.0,
        total_cost=1.0,
        cost_cpu=0.5,
        cost_ram=0.5,
        cost_source="pricing_catalog",
        cpu_unreserved_cores=1.0,
        memory_unreserved_bytes=1024.0**3,
    )


async def test_idle_rows_follow_the_raw_retention_of_pod_rows(metrics_db, clock):
    repo = _repository(metrics_db)
    steps = 12 * 24 * DAYS
    await repo.write_combined_metrics([_sample(index) for index in range(steps)])
    await repo.write_idle_metrics([_idle(index) for index in range(steps)])

    clock.now = T0 + timedelta(days=DAYS, minutes=37)
    stats = await MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None).run()

    retention_cutoff = (clock.now - timedelta(days=7)).replace(minute=0)
    (pod_min,) = (await _fetch(metrics_db, 'SELECT MIN("timestamp") FROM combined_metrics'))[0]
    (idle_min, idle_count) = (await _fetch(metrics_db, 'SELECT MIN("timestamp"), COUNT(*) FROM node_idle_metrics'))[0]
    assert _timestamp(idle_min) == retention_cutoff
    assert _timestamp(idle_min) == _timestamp(pod_min)
    kept = int((T0 + timedelta(days=DAYS) - retention_cutoff).total_seconds() // 300)
    assert idle_count == kept
    assert stats["raw_rows_pruned"] == 2 * (steps - kept)


async def test_idle_rows_are_compressed_before_they_are_pruned(metrics_db, clock):
    """ROL-07: idle rows have their own hourly aggregate; pruning never loses a step."""
    repo = _repository(metrics_db)
    steps = 12 * 24 * DAYS
    await repo.write_idle_metrics([_idle(index) for index in range(steps)])

    clock.now = T0 + timedelta(days=DAYS, minutes=37)
    await MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None).run()
    (raw,) = (await _fetch(metrics_db, "SELECT COUNT(*) FROM node_idle_metrics"))[0]
    (hourly_samples, hourly_cost) = (
        await _fetch(metrics_db, "SELECT SUM(sample_count), SUM(total_cost) FROM node_idle_metrics_hourly")
    )[0]
    assert raw < steps
    # Every pruned step lives on in the hourly aggregate, every hour with its 12 steps.
    assert hourly_samples >= steps - raw
    assert hourly_samples % 12 == 0
    assert hourly_cost == pytest.approx(float(hourly_samples))


async def test_hourly_tables_keep_the_gpu_energy_of_raw_rows(metrics_db, clock):
    repo = _repository(metrics_db)
    hours = 48
    # Within each hour: 100 W, 50 W and no GPU reading, in turn (mean 50 W over the hour).
    gpu_cycle = (100.0, 50.0, None)
    pods = [_sample(index, gpu=gpu_cycle[index % 3]) for index in range(12 * hours)]
    containers = [_sample(index, container="cuda", gpu=gpu_cycle[index % 3]) for index in range(12 * hours)]
    await repo.write_combined_metrics(pods)
    await repo.write_container_metrics(containers)

    clock.now = T0 + timedelta(hours=hours + 24, minutes=37)
    compressor = MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None)
    await compressor.run()

    for table in ("combined_metrics_hourly", "container_metrics_hourly"):
        rows = await _fetch(metrics_db, f"SELECT gpu_power_watts, duration_seconds FROM {table}")
        assert len(rows) == hours, table
        for watts, duration in rows:
            # gpu_power_watts x duration_seconds is the GPU energy of the hour's raw rows.
            assert float(watts) * float(duration) == pytest.approx(4 * (100.0 + 50.0) * 300)

    # Prune the raw hours, then re-roll one of them from the hourly tables.
    clock.now = T0 + timedelta(days=DAYS, minutes=37)
    await compressor.run()
    hour = T0 + timedelta(hours=1)
    assert (await _fetch(metrics_db, "SELECT COUNT(*) FROM combined_metrics"))[0][0] == 0
    usage = create_workload_usage_repository(metrics_db, metrics_db.db_type)
    pod_rows, container_rows, source = await usage.read_hour_inputs(hour)
    assert source == "hourly"
    rolled = rollup_rows(pod_rows, container_rows, step_seconds=300.0, source=source)
    assert rolled
    for row in rolled:
        assert row.gpu_watt_seconds == pytest.approx(4 * (100.0 + 50.0) * 300)

    # Hourly reads carry the GPU power too (``read_combined_metrics_smart`` maps the column by name).
    hourly = await repo.read_combined_metrics_smart(hour, hour + timedelta(hours=1))
    assert [metric.gpu_power_watts for metric in hourly] == [pytest.approx(50.0)]
