"""Lossless hourly compression regression tests (roadmap A3, BUG-02).

The scenario mirrors the audit simulation ``sim_compress.py``: nine days of
5-minute samples, an hourly compression job that starts at a random minute
offset, raw retention of seven days. Every closed hour must keep all of its
samples once it moved from the raw tables to the hourly tables.
"""

from __future__ import annotations

import asyncio
import os
import random
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
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

T0 = real_datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

#: Wall-clock budget of one simulated multi-day compression schedule. The compressor work
#: of a whole simulation takes a few seconds on PostgreSQL. A lock wait or a pool
#: starvation must fail the test here instead of stalling the suite.
SIMULATION_BUDGET_SECONDS = 45.0


async def _within_budget(simulation) -> None:
    """Run ``simulation`` (a coroutine) or fail when it exceeds :data:`SIMULATION_BUDGET_SECONDS`."""
    try:
        await asyncio.wait_for(simulation, timeout=SIMULATION_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        pytest.fail(f"compression simulation did not finish within {SIMULATION_BUDGET_SECONDS:.0f}s")


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
        schema = f"gk_compress_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "compress.sqlite"))
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


def _sample(index: int, *, pod: str = "p", container: str | None = None, value: float = 1.0) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="ns",
        container_name=container,
        timestamp=T0 + timedelta(minutes=5 * index),
        total_cost=value,
        co2e_grams=value,
        joules=value,
        pue=1.0,
        grid_intensity=1.0,
        cpu_request=0,
        memory_request=0,
        duration_seconds=300,
    )


async def test_sim_compress_keeps_every_sample_of_closed_hours(metrics_db, clock):
    """Port of sim_compress.py: hourly cycles at :37 must not truncate any hour."""
    repo = _repository(metrics_db)
    samples_per_hour = 12
    samples = [_sample(i) for i in range(samples_per_hour * 24 * 9)]
    samples += [_sample(i, container="app") for i in range(samples_per_hour * 24 * 9)]
    await repo.write_combined_metrics(samples)

    # The workload usage rollup (tested on its own) re-rolls 24 hours per run: across
    # ~100 simulated runs it made one PostgreSQL case take 70-95s of silence (the stall).
    compressor = MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None, rollup_enabled=False)
    start = T0 + timedelta(days=7, hours=2, minutes=37)

    async def simulate() -> None:
        for hour in range(30):
            clock.now = start + timedelta(hours=hour)
            await compressor.run()

    await _within_budget(simulate())

    for table in ("combined_metrics_hourly", "container_metrics_hourly"):
        rows = await _fetch(metrics_db, f"SELECT sample_count, total_cost, joules FROM {table} ORDER BY hour_bucket")
        assert rows, table
        partial = [row for row in rows if row[0] != samples_per_hour]
        assert partial == [], f"{table} lost samples in {len(partial)} buckets"
        assert all(row[1] == pytest.approx(12.0) and row[2] == pytest.approx(12.0) for row in rows)
        # Every closed hour up to the last run's compression horizon is present.
        last_closed = (clock.now - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
        assert len(rows) == int((last_closed - T0).total_seconds() // 3600)


@pytest.mark.parametrize("seed", [1, 7, 42])
async def test_compression_conserves_sums_with_random_job_offsets(metrics_db, clock, seed):
    """Sum of raw rows before compression equals sum of hourly rows after it."""
    rng = random.Random(seed)
    repo = _repository(metrics_db)
    samples = []
    for pod in ("a", "b"):
        for index in range(12 * 24 * 4):
            if rng.random() < 0.1:
                continue  # gaps must not break conservation
            samples.append(_sample(index, pod=pod, value=round(rng.uniform(0.0, 3.0), 4)))
    await repo.write_combined_metrics(samples)
    raw_by_bucket: dict[real_datetime, list[float]] = {}
    for sample in samples:
        assert sample.timestamp is not None
        bucket = sample.timestamp.replace(minute=0, second=0, microsecond=0)
        raw_by_bucket.setdefault(bucket, []).append(sample.total_cost)

    # The workload usage rollup (tested on its own) re-rolls 24 hours per run: across
    # ~100 simulated runs it made one PostgreSQL case take 70-95s of silence (the stall).
    compressor = MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None, rollup_enabled=False)
    clock.now = T0 + timedelta(days=1, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))

    async def simulate() -> None:
        while clock.now < T0 + timedelta(days=5):
            await compressor.run()
            clock.now += timedelta(minutes=rng.randint(40, 80), seconds=rng.randint(0, 59))

    await _within_budget(simulate())

    hourly = await _fetch(
        metrics_db,
        "SELECT hour_bucket, SUM(sample_count), SUM(total_cost), SUM(joules), SUM(co2e_grams) "
        "FROM combined_metrics_hourly GROUP BY hour_bucket ORDER BY hour_bucket",
    )
    assert hourly
    for bucket, sample_count, total_cost, joules, co2e in hourly:
        bucket_dt = bucket if isinstance(bucket, real_datetime) else real_datetime.fromisoformat(bucket)
        expected = raw_by_bucket[bucket_dt.astimezone(timezone.utc)]
        assert sample_count == len(expected)
        assert total_cost == pytest.approx(sum(expected), rel=1e-5, abs=1e-4)
        assert joules == pytest.approx(sum(expected), rel=1e-5, abs=1e-4)
        assert co2e == pytest.approx(sum(expected), rel=1e-5, abs=1e-4)


async def test_legacy_partially_pruned_first_hour_keeps_its_complete_aggregate(metrics_db, clock):
    """An upgrade from the legacy compressor must not overwrite a full hourly row with a partial raw set."""
    repo = _repository(metrics_db)
    # Raw rows for hour 0 were half pruned by the legacy, non-aligned cutoff (only :40-:55 left).
    await repo.write_combined_metrics([_sample(index) for index in range(8, 12 * 3)])
    full_bucket = CombinedMetric(
        pod_name="p",
        namespace="ns",
        timestamp=T0,
        total_cost=12.0,
        co2e_grams=12.0,
        joules=12.0,
        sample_count=12,
    )
    async with metrics_db.connection_scope() as connection:
        conn: Any = connection
        if metrics_db.db_type == "postgres":
            await conn.execute(
                "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket, sample_count, total_cost, "
                "co2e_grams, joules) VALUES ($1, $2, $3, 12, 12.0, 12.0, 12.0)",
                full_bucket.pod_name,
                full_bucket.namespace,
                T0,
            )
        else:
            await conn.execute(
                "INSERT INTO combined_metrics_hourly (pod_name, namespace, hour_bucket, sample_count, total_cost, "
                "co2e_grams, joules) VALUES (?, ?, ?, 12, 12.0, 12.0, 12.0)",
                (full_bucket.pod_name, full_bucket.namespace, "2026-10-01T00:00:00Z"),
            )
            await conn.commit()

    clock.now = T0 + timedelta(days=2, minutes=13)
    await MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None).run()

    rows = await _fetch(metrics_db, "SELECT sample_count, total_cost FROM combined_metrics_hourly ORDER BY hour_bucket")
    assert rows == [(12, pytest.approx(12.0)), (12, pytest.approx(12.0)), (12, pytest.approx(12.0))]


async def test_raw_retention_never_deletes_uncompressed_hours(metrics_db, clock, monkeypatch):
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(index) for index in range(12 * 30)])
    cfg = Config()
    cfg.METRICS_RAW_RETENTION_DAYS = 0
    clock.now = T0 + timedelta(hours=30, minutes=21)

    stats = await MetricsCompressor(metrics_db, cfg, collection_watermark_provider=None).run()

    # Only the six closed hours older than 24h were compressed, then pruned.
    assert stats["hours_compressed"] == 6
    assert stats["rows_compressed"] == 72
    assert stats["raw_rows_pruned"] == 72
    remaining = await _fetch(metrics_db, "SELECT COUNT(*) FROM combined_metrics")
    assert remaining == [(12 * 24,)]


async def test_collection_watermark_bounds_compression(metrics_db, clock):
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(index) for index in range(12 * 48)])
    clock.now = T0 + timedelta(hours=47, minutes=5)

    async def collection_watermark():
        return T0 + timedelta(hours=10, minutes=30)

    compressor = MetricsCompressor(metrics_db, Config(), collection_watermark_provider=collection_watermark)
    await compressor.run()

    assert await compressor.get_watermark("combined_metrics") == T0 + timedelta(hours=10)
    rows = await _fetch(metrics_db, "SELECT COUNT(*) FROM combined_metrics_hourly")
    assert rows == [(10,)]
