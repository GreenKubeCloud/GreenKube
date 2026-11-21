"""A workload alone in its namespace is still rated per collected hour (MEA-13 review).

The collected time of an hour is cluster-wide: a CronJob that is the only
workload of its namespace has no other row there, so the namespace alone would
call its idle steps "not collected" and extrapolate them.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.verify.model import TimeWindow, WorkloadRef
from greenkube.verify.service import ChangeMeasurementService
from greenkube.verify.strategy import MeasurementSettings

H0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
STEP = 300


def _row(namespace: str, owner: str, at: datetime) -> CombinedMetric:
    return CombinedMetric(
        pod_name=f"{owner}-1",
        namespace=namespace,
        owner_kind="CronJob" if owner == "job" else "Deployment",
        owner_name=owner,
        timestamp=at,
        duration_seconds=STEP,
        total_cost=0.001,
        cost_cpu=0.0007,
        cost_ram=0.0003,
        cost_source="opencost",
        usage_source="prometheus",
        cpu_request=500,
        memory_request=512 * 1024**2,
        cpu_usage_millicores=100,
    )


def _rows() -> list[CombinedMetric]:
    rows = []
    for hour in range(2):
        start = H0 + timedelta(hours=hour)
        # The CronJob runs 35 minutes of the first hour only; another namespace is collected all the time.
        if hour == 0:
            rows.extend(_row("batch", "job", start + timedelta(seconds=STEP * i)) for i in range(7))
        rows.extend(_row("other", "api", start + timedelta(seconds=STEP * i)) for i in range(12))
    return rows


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def repo(request, tmp_path, monkeypatch):
    schema = None
    dsn = None
    if request.param == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL tests.")
        schema = f"gk_presence_{uuid4().hex}"
        for key, value in {
            "DB_TYPE": "postgres",
            "DB_CONNECTION_STRING": dsn,
            "DB_SCHEMA": schema,
            "DB_SSL_MODE": "disable",
            "DB_POOL_MIN_SIZE": "1",
            "DB_POOL_MAX_SIZE": "2",
        }.items():
            monkeypatch.setenv(key, value)
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "presence.sqlite"))
    # Compressed hours keep their raw rows (the default 1-7 day overlap of both tables).
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "-1")
    monkeypatch.setenv("METRICS_AGGREGATED_RETENTION_DAYS", "-1")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        repository = (
            PostgresCombinedMetricsRepository(manager)
            if manager.db_type == "postgres"
            else SQLiteCombinedMetricsRepository(manager)
        )
        await repository.write_combined_metrics(_rows())
        yield repository
    finally:
        await manager.close()
        config.reload()
        if schema and dsn:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def test_a_compressed_hour_whose_raw_rows_remain_is_counted_once(repo):
    """Review: an hour in both the raw and the hourly table must not be counted twice."""
    half = H0 + timedelta(hours=3)
    await repo.write_combined_metrics([_row("half", "api", half + timedelta(seconds=STEP * i)) for i in range(6)])
    before = await repo.collected_seconds_by_hour(half, half + timedelta(hours=1), STEP)
    assert before == {half: pytest.approx(1800.0)}
    await MetricsCompressor(repo.db_manager, Config(), collection_watermark_provider=None, rollup_enabled=False).run(
        now=H0 + timedelta(days=3)
    )
    assert await repo.read_hourly_metrics(half, half + timedelta(hours=1))
    assert await repo.read_combined_metrics(half, half + timedelta(hours=1))
    after = await repo.collected_seconds_by_hour(half, half + timedelta(hours=1), STEP)
    assert after == {half: pytest.approx(1800.0)}


async def test_collected_seconds_are_read_cluster_wide(repo):
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=2))
    collected = await repo.collected_seconds_by_hour(window.start, window.end, STEP)
    assert collected == {H0: pytest.approx(3600.0), H0 + timedelta(hours=1): pytest.approx(3600.0)}


async def test_a_cronjob_alone_in_its_namespace_counts_its_idle_steps_as_zero(repo):
    service = ChangeMeasurementService.__new__(ChangeMeasurementService)
    service.combined_repo = repo
    service.config = SimpleNamespace(PROMETHEUS_QUERY_RANGE_STEP="5m")
    service.settings = MeasurementSettings()
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=2))
    _, buckets = await service._workload_buckets(WorkloadRef(namespace="batch", kind="CronJob", name="job"), window)
    first, second = buckets
    assert first.cost == pytest.approx(0.007)
    assert first.coverage == pytest.approx(1.0)
    # Collected elsewhere in the cluster, no row of the job: it did not run.
    assert second.cost == pytest.approx(0.0)
    assert second.coverage == pytest.approx(1.0)
