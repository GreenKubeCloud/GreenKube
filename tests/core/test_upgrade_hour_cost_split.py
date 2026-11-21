"""The 0.3.x upgrade hour: split and unsplit 5-minute cost rows in one hour (A.9, F-03).

0.3.x rows carry ``total_cost`` only; later rows also carry ``cost_cpu`` and ``cost_ram``.
Summing the components over an hour that holds both gives components that cover part of
the hour while the hour counts as split: the F-03 readers (``baseline_sql``,
``observed_rates_from_rows``) then dilute the CPU and RAM rates. Such an hour keeps its
total and gets NULL components, so readers price it as an unsplit hour.

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime as real_datetime
from datetime import timedelta, timezone
from typing import Any, Optional, cast
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

T0 = real_datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
#: Hour 0 mixes split and unsplit rows, hour 1 is split only, hour 2 mixes split rows with cost-free rows.
MIXED, SPLIT, FREE = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)


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
        schema = f"gk_upgrade_hour_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "upgrade_hour.sqlite"))
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


def _split(index: int) -> bool:
    """The first half of each hour is 0.3.x (unsplit), the second half carries the split."""
    return index % 12 >= 6


def _cost(hour: real_datetime, index: int) -> tuple[float, Optional[float], Optional[float]]:
    if hour == SPLIT or _split(index):
        return 1.0, 0.6, 0.3
    if hour == FREE:
        return 0.0, None, None
    return 1.0, None, None


def _row(hour: real_datetime, index: int, container: str | None = None) -> CombinedMetric:
    total, cpu, ram = _cost(hour, index)
    return CombinedMetric(
        pod_name="api-0",
        namespace="shop",
        container_name=container,
        owner_kind="StatefulSet",
        owner_name="api",
        node="n1",
        timestamp=hour + timedelta(minutes=5 * index),
        total_cost=total,
        cost_cpu=cpu,
        cost_ram=ram,
        cost_source="opencost",
        co2e_grams=1.0,
        joules=1.0,
        pue=1.0,
        grid_intensity=1.0,
        cpu_request=1000,
        memory_request=1024**3,
        cpu_usage_millicores=500,
        memory_usage_bytes=1024**3,
        duration_seconds=300,
    )


def _idle(hour: real_datetime, index: int) -> NodeIdleMetric:
    total, cpu, ram = _cost(hour, index)
    return NodeIdleMetric(
        node="n1",
        timestamp=hour + timedelta(minutes=5 * index),
        duration_seconds=300,
        joules=1.0,
        co2e_grams=1.0,
        embodied_co2e_grams=1.0,
        total_cost=total,
        # Stored idle rows may hold NULL components; the dataclass types them as floats.
        cost_cpu=cast(float, cpu),
        cost_ram=cast(float, ram),
        cost_source="opencost",
        cpu_unreserved_cores=1.0,
        memory_unreserved_bytes=1024.0**3,
    )


async def _write(manager: DatabaseManager) -> None:
    repo = _repository(manager)
    rows = [_row(hour, i) for hour in (MIXED, SPLIT, FREE) for i in range(12)]
    rows += [_row(hour, i, container="app") for hour in (MIXED, SPLIT, FREE) for i in range(12)]
    await repo.write_combined_metrics(rows)
    await repo.write_idle_metrics([_idle(hour, i) for hour in (MIXED, SPLIT, FREE) for i in range(12)])


def _hour_of(value: Any) -> int:
    parsed = value if isinstance(value, real_datetime) else real_datetime.fromisoformat(str(value))
    parsed = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return int((parsed - T0).total_seconds() // 3600)


async def test_compression_nulls_the_components_of_an_hour_mixing_split_and_unsplit_rows(metrics_db, clock):
    await _write(metrics_db)
    clock.now = T0 + timedelta(days=2)
    await MetricsCompressor(metrics_db, Config(), collection_watermark_provider=None).run()

    for table in ("combined_metrics_hourly", "container_metrics_hourly", "node_idle_metrics_hourly"):
        rows = await _fetch(metrics_db, f"SELECT hour_bucket, total_cost, cost_cpu, cost_ram FROM {table}")
        by_hour = {_hour_of(row[0]): row[1:] for row in rows}
        assert set(by_hour) == {0, 1, 2}, table
        total, cpu, ram = by_hour[0]
        assert float(total) == pytest.approx(12.0), table
        assert (cpu, ram) == (None, None), table
        total, cpu, ram = by_hour[1]
        assert (float(total), float(cpu), float(ram)) == pytest.approx((12.0, 7.2, 3.6)), table
        # Cost-free rows without a split do not make the hour unsplit.
        total, cpu, ram = by_hour[2]
        assert (float(total), float(cpu), float(ram)) == pytest.approx((6.0, 3.6, 1.8)), table

    trusted = await _fetch(
        metrics_db, "SELECT hour_bucket, trusted_cost_cpu, trusted_cost_ram FROM combined_metrics_hourly"
    )
    assert {_hour_of(row[0]): row[1:] for row in trusted}[0] == (None, None)


async def test_workload_rollup_nulls_the_components_of_a_mixed_hour(metrics_db):
    await _write(metrics_db)
    usage = create_workload_usage_repository(metrics_db, metrics_db.db_type)
    for hour, expected in ((MIXED, (12.0, None, None)), (SPLIT, (12.0, 7.2, 3.6)), (FREE, (6.0, 3.6, 1.8))):
        pod_rows, container_rows, source = await usage.read_hour_inputs(hour)
        assert source == "raw"
        rolled = rollup_rows(pod_rows, container_rows, step_seconds=300.0, source=source)
        assert rolled
        for row in rolled:
            assert row.total_cost == pytest.approx(expected[0])
            assert row.cost_cpu == pytest.approx(expected[1])
            assert row.cost_ram == pytest.approx(expected[2])
