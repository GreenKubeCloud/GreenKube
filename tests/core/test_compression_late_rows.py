"""Hours that receive rows after their compression are re-aggregated (ROL-02, STO-02, RECON-02).

Rows written, re-written or deleted in an hour below the compression watermark
(``greenkube start --last``, a retried failed step, an OpenCost re-pricing, a
recompute) mark the hour dirty; the next compression run rebuilds its hourly
rows from the raw rows while they are complete. Compression never passes the
collection watermark nor a failed or cost-pending step.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.utils.date_utils import to_iso_z

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


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
        schema = f"gk_late_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "late.sqlite"))
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


async def _fetch(manager: DatabaseManager, query: str, *args: Any) -> list[tuple]:
    async with manager.connection_scope() as connection:
        conn: Any = connection
        if manager.db_type == "postgres":
            return [tuple(row) for row in await conn.fetch(query, *args)]
        params = tuple(to_iso_z(a) if isinstance(a, datetime) else a for a in args)
        async with conn.execute(query.replace("$1", "?").replace("$2", "?"), params) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def _execute(manager: DatabaseManager, query: str, *args: Any) -> None:
    async with manager.connection_scope() as connection:
        conn: Any = connection
        if manager.db_type == "postgres":
            await conn.execute(query, *args)
            return
        params = tuple(to_iso_z(a) if isinstance(a, datetime) else a for a in args)
        await conn.execute(query.replace("$1", "?").replace("$2", "?"), params)
        await conn.commit()


def _sample(index: int, *, pod: str = "p", value: float = 1.0, container: str | None = None) -> CombinedMetric:
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


async def _no_watermark():
    return None


def _compressor(manager: DatabaseManager, cfg: Config | None = None, provider=_no_watermark) -> MetricsCompressor:
    return MetricsCompressor(manager, cfg or Config(), collection_watermark_provider=provider, rollup_enabled=False)


async def _hour_total(manager: DatabaseManager, table: str, hour: datetime) -> tuple:
    rows = await _fetch(
        manager,
        f"SELECT COALESCE(SUM(sample_count), 0), COALESCE(SUM(total_cost), 0) FROM {table} WHERE hour_bucket = $1",
        hour,
    )
    return rows[0]


async def test_late_and_rewritten_rows_reach_the_compressed_hour(metrics_db):
    """Acceptance 1 and 2: late rows of a new pod and re-written rows of a known pod are re-aggregated."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 48)])
    await repo.write_combined_metrics([_sample(i, container="app") for i in range(12 * 48)])
    compressor = _compressor(metrics_db)
    await compressor.run(now=T0 + timedelta(hours=40, minutes=7))
    hour = T0 + timedelta(hours=5)
    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (12, pytest.approx(12.0))

    # 12 late rows of a new pod and the 12 rows of pod p re-written with 10.0 each.
    await repo.write_combined_metrics([_sample(60 + i, pod="late", value=5.0) for i in range(12)])
    await repo.write_combined_metrics([_sample(60 + i, value=10.0) for i in range(12)])
    await repo.write_combined_metrics([_sample(60 + i, value=3.0, container="app") for i in range(12)])
    await compressor.run(now=T0 + timedelta(hours=41, minutes=7))

    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (24, pytest.approx(180.0))
    assert await _hour_total(metrics_db, "container_metrics_hourly", hour) == (12, pytest.approx(36.0))
    summary = await repo.aggregate_summary(hour, hour + timedelta(hours=1))
    assert summary["total_cost"] == pytest.approx(180.0)

    # Everything ever written is still in the hourly table once raw rows are pruned.
    await compressor.run(now=T0 + timedelta(days=9))
    assert (await _fetch(metrics_db, "SELECT COUNT(*) FROM combined_metrics"))[0][0] == 0
    total = await _fetch(metrics_db, "SELECT SUM(total_cost) FROM combined_metrics_hourly")
    assert total[0][0] == pytest.approx(12 * 48 - 12 + 120 + 60)


async def test_rows_removed_from_a_compressed_hour_leave_its_aggregate(metrics_db):
    """A window replacement that deletes a pod's rows in a compressed hour rebuilds that hour."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    await repo.write_combined_metrics([_sample(i, pod="gone", value=2.0) for i in range(12 * 30)])
    compressor = _compressor(metrics_db)
    await compressor.run(now=T0 + timedelta(hours=29))
    hour = T0 + timedelta(hours=2)
    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (24, pytest.approx(36.0))

    await _execute(
        metrics_db,
        "DELETE FROM combined_metrics WHERE pod_name = 'gone' AND timestamp >= $1 AND timestamp < $2",
        hour,
        hour + timedelta(hours=1),
    )
    await compressor.run(now=T0 + timedelta(hours=29, minutes=30))
    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (12, pytest.approx(12.0))


async def test_late_rows_in_a_pruned_hour_keep_the_complete_aggregate(metrics_db):
    """Raw rows of an hour already pruned are incomplete: the hourly row is kept, never replaced."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 24 * 9)])
    compressor = _compressor(metrics_db)
    await compressor.run(now=T0 + timedelta(days=9))
    hour = T0 + timedelta(hours=5)
    assert (await _fetch(metrics_db, "SELECT COUNT(*) FROM combined_metrics WHERE timestamp < $1", hour))[0][0] == 0

    await repo.write_combined_metrics([_sample(60, value=50.0)])
    await compressor.run(now=T0 + timedelta(days=9, hours=1))
    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (12, pytest.approx(12.0))
    assert (await _fetch(metrics_db, "SELECT COUNT(*) FROM metrics_dirty_hours"))[0][0] == 0

    # The usage rollup reads the complete aggregate, not the lone late row.
    from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

    await repo.write_combined_metrics([_sample(61, value=50.0)])
    usage = create_workload_usage_repository(metrics_db, metrics_db.db_type)
    pods, _, source = await usage.read_hour_inputs(hour)
    assert source == "hourly"
    assert len(pods) == 1


async def test_compression_waits_for_the_collection_watermark(metrics_db):
    """Acceptance 3 (sim_compress): the 09:00 hour is compressed only after it is collected."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i, value=0.02) for i in range(12 * 9)])
    collected = {"until": T0 + timedelta(hours=9)}

    async def provider():
        return collected["until"]

    compressor = _compressor(metrics_db, provider=provider)
    await compressor.run(now=T0 + timedelta(days=1, hours=10, minutes=30))
    assert await compressor.get_watermark("combined_metrics") == T0 + timedelta(hours=9)

    await repo.write_combined_metrics([_sample(12 * 9 + i, value=0.02) for i in range(12)])
    collected["until"] = T0 + timedelta(hours=10)
    await compressor.run(now=T0 + timedelta(days=1, hours=10, minutes=35))
    hour = T0 + timedelta(hours=9)
    assert await _hour_total(metrics_db, "combined_metrics_hourly", hour) == (12, pytest.approx(0.24))
    summary = await repo.aggregate_summary(hour, hour + timedelta(hours=1))
    assert summary["total_cost"] == pytest.approx(0.24)


async def test_the_scheduled_limit_stops_at_failed_and_cost_pending_steps(metrics_db):
    """STO-02: the production provider returns the collection watermark lowered to the first unsettled step."""
    from greenkube.core.collection import METRICS_WATERMARK_SOURCE, collection_cluster_key
    from greenkube.core.metrics_compressor import collection_limit_provider
    from greenkube.storage.base_collection_watermark_repository import create_collection_watermark_repository
    from greenkube.storage.base_collection_window_repository import (
        COST_STATUS_PENDING,
        STATUS_COLLECTED,
        STATUS_FAILED,
        CollectionWindowRecord,
        create_collection_window_repository,
    )

    cfg = Config()
    provider = collection_limit_provider(metrics_db, cfg)
    assert await provider() == datetime(1970, 1, 1, tzinfo=timezone.utc)

    cluster = collection_cluster_key(cfg)
    await create_collection_watermark_repository(metrics_db, metrics_db.db_type).advance(
        cluster, METRICS_WATERMARK_SOURCE, T0 + timedelta(hours=12)
    )
    assert await provider() == T0 + timedelta(hours=12)

    def step(start: datetime, status: str, cost_status: str | None = None) -> CollectionWindowRecord:
        return CollectionWindowRecord(
            cluster_uid=cluster,
            source=METRICS_WATERMARK_SOURCE,
            window_start=start,
            window_end=start + timedelta(minutes=5),
            status=status,
            expected_steps=1,
            cost_status=cost_status,
        )

    windows = create_collection_window_repository(metrics_db, metrics_db.db_type)
    await windows.record([step(T0 + timedelta(hours=10, minutes=5), STATUS_COLLECTED, COST_STATUS_PENDING)])
    assert await provider() == T0 + timedelta(hours=10, minutes=5)
    await windows.record([step(T0 + timedelta(hours=9, minutes=30), STATUS_FAILED)])
    assert await provider() == T0 + timedelta(hours=9, minutes=30)

    await _repository(metrics_db).write_combined_metrics([_sample(i) for i in range(12 * 13)])
    compressor = _compressor(metrics_db, provider=provider)
    await compressor.run(now=T0 + timedelta(days=2))
    assert await compressor.get_watermark("combined_metrics") == T0 + timedelta(hours=9)


def test_the_collection_watermark_provider_is_mandatory():
    with pytest.raises(TypeError):
        MetricsCompressor(object(), Config())  # type: ignore[call-arg]


def test_a_backfill_longer_than_the_compression_age_raises_the_effective_age():
    """Acceptance 4, relaxed on review: the effective compression age is max(age, backfill), never a refusal."""
    cfg = Config(COLLECTION_MAX_BACKFILL="72h", METRICS_COMPRESSION_AGE_HOURS=24)
    assert cfg.metrics_compression_age == timedelta(hours=72)
    assert Config(COLLECTION_MAX_BACKFILL="30m", METRICS_COMPRESSION_AGE_HOURS=6).metrics_compression_age == timedelta(
        hours=6
    )
    with pytest.raises(ValueError, match="METRICS_COMPRESSION_AGE_HOURS"):
        Config(METRICS_COMPRESSION_AGE_HOURS=0, COLLECTION_MAX_BACKFILL="10m")


async def test_compression_waits_for_the_backfill_horizon(metrics_db):
    """An hour inside the backfill horizon is never compressed, even past METRICS_COMPRESSION_AGE_HOURS."""
    await _repository(metrics_db).write_combined_metrics([_sample(i) for i in range(12 * 30)])
    cfg = Config(COLLECTION_MAX_BACKFILL="72h", METRICS_COMPRESSION_AGE_HOURS=24)
    compressor = _compressor(metrics_db, cfg)
    await compressor.run(now=T0 + timedelta(hours=48))
    assert await compressor.get_watermark("combined_metrics") is None
    await compressor.run(now=T0 + timedelta(hours=80))
    assert await compressor.get_watermark("combined_metrics") == T0 + timedelta(hours=8)


@pytest.mark.parametrize("backfill", ["3600s", "30m"])
async def test_the_scheduled_limit_reads_every_backfill_format(metrics_db, backfill):
    """A failed step older than the backfill horizon no longer holds compression back ('30m' is not 30 months)."""
    from greenkube.core.collection import METRICS_WATERMARK_SOURCE, collection_cluster_key
    from greenkube.core.metrics_compressor import collection_limit_provider
    from greenkube.storage.base_collection_watermark_repository import create_collection_watermark_repository
    from greenkube.storage.base_collection_window_repository import (
        STATUS_FAILED,
        CollectionWindowRecord,
        create_collection_window_repository,
    )

    cfg = Config(COLLECTION_MAX_BACKFILL=backfill)
    cluster = collection_cluster_key(cfg)
    await create_collection_watermark_repository(metrics_db, metrics_db.db_type).advance(
        cluster, METRICS_WATERMARK_SOURCE, T0 + timedelta(hours=12)
    )
    old_failure = CollectionWindowRecord(
        cluster_uid=cluster,
        source=METRICS_WATERMARK_SOURCE,
        window_start=T0 + timedelta(hours=10),
        window_end=T0 + timedelta(hours=10, minutes=5),
        status=STATUS_FAILED,
        expected_steps=1,
    )
    await create_collection_window_repository(metrics_db, metrics_db.db_type).record([old_failure])
    assert await collection_limit_provider(metrics_db, cfg)() == T0 + timedelta(hours=12)


async def test_a_legacy_first_hour_without_watermark_keeps_its_aggregate(metrics_db):
    """An install upgraded without a watermark row: late rows never rebuild its partly pruned first hour."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    compressor = _compressor(metrics_db)
    await compressor.run(now=T0 + timedelta(hours=29))
    # The state of an install compressed by the legacy compressor: no watermark row, and the
    # first raw hour partly pruned at a non-aligned cutoff while its aggregate is complete.
    await _execute(metrics_db, "DELETE FROM metrics_compression_watermarks")
    await _execute(metrics_db, "DELETE FROM combined_metrics WHERE timestamp < $1", T0 + timedelta(minutes=40))
    await _execute(metrics_db, "DELETE FROM metrics_dirty_hours")
    await compressor.run(now=T0 + timedelta(hours=29))
    assert await compressor.get_watermark("combined_metrics") == T0 + timedelta(hours=5)

    await repo.write_combined_metrics([_sample(2, value=50.0)])
    await compressor.run(now=T0 + timedelta(hours=29, minutes=30))
    assert await _hour_total(metrics_db, "combined_metrics_hourly", T0) == (12, pytest.approx(12.0))


async def test_moving_a_row_between_compressed_hours_marks_both_hours(metrics_db):
    """An UPDATE that changes a raw row's timestamp marks its old hour as well as its new one."""
    repo = _repository(metrics_db)
    await repo.write_combined_metrics([_sample(i) for i in range(12 * 30)])
    await _compressor(metrics_db).run(now=T0 + timedelta(hours=29))
    await _execute(
        metrics_db,
        "UPDATE combined_metrics SET timestamp = $1 WHERE timestamp = $2",
        T0 + timedelta(hours=3, minutes=2),
        T0 + timedelta(hours=1, minutes=5),
    )
    marked = await _fetch(metrics_db, "SELECT hour_bucket FROM metrics_dirty_hours ORDER BY hour_bucket")
    hours = [row[0] if isinstance(row[0], str) else to_iso_z(row[0]) for row in marked]
    assert hours == [to_iso_z(T0 + timedelta(hours=1)), to_iso_z(T0 + timedelta(hours=3))]
