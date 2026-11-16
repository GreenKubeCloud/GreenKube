"""Readers split raw and hourly rows at the compression watermark (ROL-01, RECON-03, API-03, MEA-18).

Three days of 5-minute rows, one compression run: every reader that combines
``combined_metrics`` and ``combined_metrics_hourly`` must return the sum of the
raw rows of its window, whatever the minute of the request, the alignment of
the window start or the position of the compression watermark.
"""

from __future__ import annotations

import os
import random
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
from greenkube.storage.raw_hourly_split import split_window
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.utils.date_utils import to_iso_z

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
T0 = NOW.replace(minute=0) - timedelta(days=3)


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
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL seam tests.")
        schema = f"gk_seam_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "seam.sqlite"))
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


def _rows(seed: int = 0) -> list[CombinedMetric]:
    rng = random.Random(seed)
    rows = []
    step = 0
    while T0 + timedelta(minutes=5 * step) < NOW:
        for pod in ("a", "b"):
            value = round(rng.uniform(0.5, 2.0), 3)
            rows.append(
                CombinedMetric(
                    pod_name=pod,
                    namespace="ns" if pod == "a" else "other",
                    timestamp=T0 + timedelta(minutes=5 * step),
                    total_cost=value,
                    co2e_grams=value,
                    joules=value,
                    pue=1.0,
                    grid_intensity=1.0,
                    cpu_request=0,
                    memory_request=0,
                    duration_seconds=300,
                )
            )
        step += 1
    return rows


def _raw_sum(rows: list[CombinedMetric], start: datetime, end: datetime, namespace: str | None = None) -> float:
    return sum(
        r.total_cost
        for r in rows
        if r.timestamp is not None and start <= r.timestamp < end and (namespace is None or r.namespace == namespace)
    )


async def _all_totals(repo, start: datetime, end: datetime, namespace: str | None = None) -> dict[str, float]:
    summary = await repo.aggregate_summary(start, end, namespace)
    series = await repo.aggregate_timeseries(start, end, "day", namespace)
    by_ns = await repo.aggregate_by_namespace(start, end, namespace)
    top = await repo.aggregate_top_pods(start, end, namespace, limit=50)
    smart = await repo.read_combined_metrics_smart(start, end, namespace)
    containers = await repo.read_container_metrics_smart(start, end, namespace)
    totals = {
        "summary": summary["total_cost"],
        "timeseries": sum(point["total_cost"] for point in series),
        "by_namespace": sum(row["total_cost"] for row in by_ns),
        "top_pods": sum(row["total_cost"] for row in top),
        "smart": sum(m.total_cost for m in smart),
        "container_smart": sum(m.total_cost for m in containers),
    }
    if hasattr(repo, "read_combined_metrics_page") and repo.db_manager.db_type == "postgres":
        _, page = await repo.read_combined_metrics_page(start, end, namespace, 0, 100000)
        totals["page"] = sum(m.total_cost for m in page)
    return totals


async def _write(repo, rows: list[CombinedMetric]) -> None:
    await repo.write_combined_metrics(rows)
    await repo.write_container_metrics([row.model_copy(update={"container_name": "app"}) for row in rows])


async def test_summary_covers_every_raw_row_after_compression(metrics_db):
    """Acceptance 1 and 2: whole window and a start that is not hour-aligned."""
    repo = _repository(metrics_db)
    rows = _rows()
    await _write(repo, rows)
    await MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None).run(now=NOW)

    for start in (T0, NOW - timedelta(days=2, minutes=17)):
        expected = _raw_sum(rows, start, NOW + timedelta(minutes=1))
        totals = await _all_totals(repo, start, NOW + timedelta(minutes=1))
        for name, total in totals.items():
            assert total == pytest.approx(expected, rel=1e-9), (name, start)


async def test_totals_unchanged_when_the_watermark_lags(metrics_db):
    """Acceptance 3: watermark 6 h back and its hourly buckets deleted, totals unchanged."""
    repo = _repository(metrics_db)
    rows = _rows(1)
    await _write(repo, rows)
    await MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None).run(now=NOW)
    expected = _raw_sum(rows, T0, NOW + timedelta(minutes=1))

    async with metrics_db.connection_scope() as connection:
        conn: Any = connection
        if metrics_db.db_type == "postgres":
            watermark = await conn.fetchval(
                "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'"
            )
            back = watermark - timedelta(hours=6)
            for table in ("combined_metrics", "container_metrics"):
                await conn.execute(
                    "UPDATE metrics_compression_watermarks SET compressed_until = $1 WHERE table_name = $2",
                    back,
                    table,
                )
                await conn.execute(f"DELETE FROM {table}_hourly WHERE hour_bucket >= $1", back)
        else:
            async with conn.execute(
                "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = 'combined_metrics'"
            ) as cursor:
                value = (await cursor.fetchone())[0]
            back = datetime.fromisoformat(value.replace("Z", "+00:00")) - timedelta(hours=6)
            for table in ("combined_metrics", "container_metrics"):
                await conn.execute(
                    "UPDATE metrics_compression_watermarks SET compressed_until = ? WHERE table_name = ?",
                    (to_iso_z(back), table),
                )
                await conn.execute(f"DELETE FROM {table}_hourly WHERE hour_bucket >= ?", (to_iso_z(back),))
            await conn.commit()

    totals = await _all_totals(repo, T0, NOW + timedelta(minutes=1))
    for name, total in totals.items():
        assert total == pytest.approx(expected, rel=1e-9), name


@pytest.mark.parametrize("seed", [3, 11])
async def test_random_windows_cover_every_raw_row_exactly_once(metrics_db, seed):
    """Acceptance 4: random windows and namespaces, compared with the raw sum while raw is unpruned."""
    rng = random.Random(seed)
    repo = _repository(metrics_db)
    rows = _rows(seed)
    await _write(repo, rows)
    await MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None).run(
        now=NOW - timedelta(minutes=rng.randint(0, 600))
    )
    span = int((NOW - T0).total_seconds() // 60)
    for _ in range(6):
        a, b = sorted(rng.sample(range(span + 2), 2))
        start, end = T0 + timedelta(minutes=a), T0 + timedelta(minutes=b)
        namespace = rng.choice([None, "ns"])
        expected = _raw_sum(rows, start, end, namespace)
        summary = await repo.aggregate_summary(start, end, namespace)
        assert summary["total_cost"] == pytest.approx(expected, rel=1e-9, abs=1e-9), (start, end, namespace)
        smart = await repo.read_combined_metrics_smart(start, end, namespace)
        assert sum(m.total_cost for m in smart) == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_split_window_is_hour_aligned_and_disjoint():
    boundary = datetime(2026, 10, 2, 5, tzinfo=timezone.utc)
    split = split_window(
        datetime(2026, 10, 1, 3, 17, tzinfo=timezone.utc), datetime(2026, 10, 2, 9, 40, tzinfo=timezone.utc), boundary
    )
    assert split.hourly_start == datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
    assert split.hourly_end == boundary
    assert split.raw_ranges == (
        (datetime(2026, 10, 1, 3, 17, tzinfo=timezone.utc), datetime(2026, 10, 1, 4, tzinfo=timezone.utc)),
        (boundary, datetime(2026, 10, 2, 9, 40, tzinfo=timezone.utc)),
    )
    assert not split_window(boundary, boundary + timedelta(hours=3), None).has_hourly


async def test_compression_lag_over_two_hours_is_degraded(metrics_db):
    """Acceptance 5: the lag is exported and a lag over 2 h reports degraded."""
    from greenkube.core.observability import REGISTRY

    repo = _repository(metrics_db)
    await _write(repo, _rows())
    compressor = MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None)
    await compressor.run(now=NOW)
    assert (await compressor.compression_status(NOW))["status"] == "ok"
    late = await compressor.compression_status(NOW + timedelta(hours=3))
    assert late["status"] == "degraded"
    assert late["lag_seconds"] >= 3 * 3600 - 60
    gauge = REGISTRY.get_sample_value("greenkube_metrics_compression_lag_seconds", {"table": "combined_metrics"})
    assert gauge is not None and gauge >= 3 * 3600 - 60


async def test_a_failed_compression_step_marks_the_loop_tick_failed(metrics_db, monkeypatch):
    """The compressor swallows its errors but reports them to the loop registry."""
    import greenkube.core.loops as loops

    reported: list = []
    monkeypatch.setattr(loops, "report_loop_failure", lambda error=None: reported.append(error))
    compressor = MetricsCompressor(metrics_db, Config(), rollup_enabled=False, collection_watermark_provider=None)

    async def boom(now=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(compressor, "_compress_to_hourly", boom)
    await compressor.run(now=NOW)
    assert len(reported) == 1 and isinstance(reported[0], RuntimeError)
