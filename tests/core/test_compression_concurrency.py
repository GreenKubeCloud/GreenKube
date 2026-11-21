"""Compression and dirty-hour rebuilds never lose a write committed concurrently (PostgreSQL, review).

A writer whose transaction is open while the compressor closes or rebuilds its
hour must end in the hourly table: the compressor waits for it (row locks on
the watermark and on the dirty mark), or the hour is marked again.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

import greenkube.core.metrics_compressor as compressor_module
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def pg(monkeypatch):
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL concurrency tests.")
    schema = f"gk_conc_{uuid4().hex}"
    for key, value in {
        "METRICS_COMPRESSION_AGE_HOURS": "24",
        "METRICS_RAW_RETENTION_DAYS": "7",
        "PROMETHEUS_QUERY_RANGE_STEP": "5m",
        "DB_TYPE": "postgres",
        "DB_CONNECTION_STRING": dsn,
        "DB_SCHEMA": schema,
        "DB_SSL_MODE": "disable",
        "DB_POOL_MIN_SIZE": "1",
        "DB_POOL_MAX_SIZE": "2",
    }.items():
        monkeypatch.setenv(key, value)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    writer = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        yield manager, writer
    finally:
        await writer.close()
        await manager.close()
        config.reload()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


def _sample(index: int, *, pod: str = "p", value: float = 1.0) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="ns",
        timestamp=T0 + timedelta(minutes=5 * index),
        total_cost=value,
        co2e_grams=value,
        joules=value,
        cpu_request=0,
        memory_request=0,
        duration_seconds=300,
    )


def _compressor(manager: DatabaseManager) -> MetricsCompressor:
    return MetricsCompressor(manager, Config(), collection_watermark_provider=None, rollup_enabled=False)


async def _hour_vs_raw(writer: asyncpg.Connection, hour: datetime) -> tuple[float, float]:
    hourly = await writer.fetchval(
        "SELECT COALESCE(SUM(total_cost), 0) FROM combined_metrics_hourly WHERE hour_bucket = $1", hour
    )
    raw = await writer.fetchval(
        "SELECT COALESCE(SUM(total_cost), 0) FROM combined_metrics WHERE timestamp >= $1 AND timestamp < $2",
        hour,
        hour + timedelta(hours=1),
    )
    return float(hourly or 0.0), float(raw or 0.0)


async def _while_open(writer: asyncpg.Connection, statement: str, *args, action) -> None:
    """Run ``action`` while ``writer`` holds ``statement`` uncommitted, then commit it."""
    transaction = writer.transaction()
    await transaction.start()
    await writer.execute(statement, *args)
    task = asyncio.create_task(action())
    await asyncio.sleep(0.5)
    await transaction.commit()
    await asyncio.wait_for(task, timeout=20)


async def test_a_write_committed_during_a_dirty_hour_rebuild_is_not_lost(pg):
    manager, writer = pg
    repo = PostgresCombinedMetricsRepository(manager)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    compressor = _compressor(manager)
    await compressor.run(now=T0 + timedelta(hours=29))
    hour = T0 + timedelta(hours=2)
    await repo.write_combined_metrics([_sample(24, pod="late")])  # hour 2 is now marked dirty

    await _while_open(
        writer,
        "UPDATE combined_metrics SET total_cost = 100 WHERE pod_name = 'p' AND timestamp = $1",
        hour + timedelta(minutes=5),
        action=compressor._reaggregate_dirty_hours,
    )
    await compressor._reaggregate_dirty_hours()
    hourly, raw = await _hour_vs_raw(writer, hour)
    assert raw == pytest.approx(112.0)
    assert hourly == pytest.approx(raw)


async def test_a_write_committed_while_its_hour_is_compressed_is_not_lost(pg):
    manager, writer = pg
    repo = PostgresCombinedMetricsRepository(manager)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    compressor = _compressor(manager)
    await compressor.run(now=T0 + timedelta(hours=26))
    hour = T0 + timedelta(hours=2)
    assert await compressor.get_watermark("combined_metrics") == hour

    await _while_open(
        writer,
        "UPDATE combined_metrics SET total_cost = 100 WHERE pod_name = 'p' AND timestamp = $1",
        hour + timedelta(minutes=5),
        action=lambda: compressor._compress_to_hourly(T0 + timedelta(hours=27)),
    )
    await compressor._reaggregate_dirty_hours()
    hourly, raw = await _hour_vs_raw(writer, hour)
    assert raw == pytest.approx(111.0)
    assert hourly == pytest.approx(raw)


async def test_a_write_committed_during_the_first_compression_is_not_lost(pg):
    """No watermark row has been written yet: the seeded row still makes the compressor wait for the writer."""
    manager, writer = pg
    repo = PostgresCombinedMetricsRepository(manager)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    compressor = _compressor(manager)
    hour = T0 + timedelta(hours=2)

    await _while_open(
        writer,
        "UPDATE combined_metrics SET total_cost = 51 WHERE pod_name = 'p' AND timestamp = $1",
        hour + timedelta(minutes=5),
        action=lambda: compressor._compress_to_hourly(T0 + timedelta(hours=27)),
    )
    await compressor._reaggregate_dirty_hours()
    hourly, raw = await _hour_vs_raw(writer, hour)
    assert raw == pytest.approx(62.0)
    assert hourly == pytest.approx(raw)


@pytest.mark.parametrize("statement_timeout_ms", ["1500", "0"], ids=["statement-timeout", "no-statement-timeout"])
async def test_a_writer_left_open_delays_compression_by_one_cycle_and_never_hangs_it(
    pg, monkeypatch, statement_timeout_ms
):
    """A raw writer that never commits (a stuck client) holds the watermark row FOR SHARE.

    The compressor's lock wait is bounded by ``DB_STATEMENT_TIMEOUT_MS``, and by its own
    ``lock_timeout`` when the statement timeout is disabled (0): the cycle reports the failure
    and returns, and the next cycle compresses the hour with the writer's committed row.
    """
    _, writer = pg
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", statement_timeout_ms)
    monkeypatch.setattr(compressor_module, "COMPRESSOR_LOCK_TIMEOUT_MS", 1500)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repo = PostgresCombinedMetricsRepository(manager)
        await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
        compressor = _compressor(manager)
        hour = T0 + timedelta(hours=2)
        transaction = writer.transaction()
        await transaction.start()
        await writer.execute(
            "UPDATE combined_metrics SET total_cost = 51 WHERE pod_name = 'p' AND timestamp = $1",
            hour + timedelta(minutes=5),
        )
        stats = await asyncio.wait_for(compressor.run(now=T0 + timedelta(hours=27)), timeout=20)
        assert stats["hours_compressed"] == 0
        assert await compressor.get_watermark("combined_metrics") is None
        await transaction.commit()

        stats = await asyncio.wait_for(compressor.run(now=T0 + timedelta(hours=27)), timeout=20)
        assert stats["hours_compressed"] > 0
        hourly, raw = await _hour_vs_raw(writer, hour)
        assert raw == pytest.approx(62.0)
        assert hourly == pytest.approx(raw)
    finally:
        await manager.close()
