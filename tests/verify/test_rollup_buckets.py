"""Hour buckets built from compressed rows equal the buckets of the raw rows (ROL-03, MEA-05, MEA-04).

A rollout replaces the workload's pod in the middle of an hour; one cost
sample of another hour is untrusted. The bucket a change measurement computes
must not depend on whether the hour was compressed.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.verify.model import TimeWindow
from greenkube.verify.series import MIN_HOUR_COVERAGE, hourly_buckets, untrusted_cost_hours

H0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
STEP = 300


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")
    monkeypatch.setenv("METRICS_RAW_RETENTION_DAYS", "-1")
    monkeypatch.setenv("METRICS_AGGREGATED_RETENTION_DAYS", "-1")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL rollup tests.")
        schema = f"gk_rollup_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "rollup.sqlite"))
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


def _row(pod: str, at: datetime, *, cost: float = 1.0, source: str = "opencost") -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        timestamp=at,
        duration_seconds=STEP,
        total_cost=cost,
        cost_cpu=cost * 0.75,
        cost_ram=cost * 0.25,
        cost_source=source,
        usage_source="prometheus",
        cpu_request=500,
        memory_request=512 * 1024**2,
        cpu_usage_millicores=100,
        joules=10.0,
        co2e_grams=1.0,
    )


def _steps(hour: datetime, first: int, last: int) -> list[datetime]:
    return [hour + timedelta(seconds=STEP * index) for index in range(first, last)]


async def _buckets_before_and_after(db, rows: list[CombinedMetric], hours: int):
    repo = _repository(db)
    await repo.write_combined_metrics(rows)
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=hours))
    raw = hourly_buckets(await repo.read_combined_metrics(window.start, window.end), window, STEP)
    await MetricsCompressor(db, Config(), collection_watermark_provider=None, rollup_enabled=False).run(
        now=H0 + timedelta(days=3)
    )
    hourly_rows = await repo.read_hourly_metrics(window.start, window.end)
    assert hourly_rows and all(row.sample_count >= 1 for row in hourly_rows)
    compressed = hourly_buckets(hourly_rows, window, STEP)
    return raw, compressed


def _assert_same(raw, compressed):
    for before, after in zip(raw, compressed):
        for field in ("coverage", "replicas", "core_hours", "cost", "cost_coverage", "cost_cpu"):
            expected, actual = getattr(before, field), getattr(after, field)
            if expected is None:
                assert actual is None, (before.hour, field)
            else:
                assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12), (before.hour, field)


async def test_a_rollout_hour_has_the_same_bucket_before_and_after_compression(db):
    """Acceptance 1: old pod :00-:30, new pod :30-:60 at 1.0 per step."""
    rows = [_row("api-old", t) for t in _steps(H0, 0, 6)] + [_row("api-new", t) for t in _steps(H0, 6, 12)]
    raw, compressed = await _buckets_before_and_after(db, rows, 1)
    assert raw[0].cost == pytest.approx(12.0)
    assert raw[0].replicas == pytest.approx(1.0)
    assert raw[0].core_hours == pytest.approx(0.5)
    _assert_same(raw, compressed)


async def test_one_untrusted_sample_keeps_the_trusted_part_of_the_hour(db):
    """MEA-05: 11 OpenCost samples and one custom-rate sample at 0.001 each."""
    rows = [_row("api", t, cost=0.001) for t in _steps(H0, 0, 11)]
    rows.append(_row("api", H0 + timedelta(seconds=STEP * 11), cost=0.5, source="custom_rate"))
    raw, compressed = await _buckets_before_and_after(db, rows, 1)
    assert raw[0].cost == pytest.approx(0.012)
    assert raw[0].cost_coverage == pytest.approx(11 / 12)
    _assert_same(raw, compressed)


@pytest.mark.parametrize("seed", [2, 5, 9])
async def test_random_contiguous_pod_lifetimes_give_the_same_buckets(db, seed):
    """Acceptance 2: random contiguous lifetimes inside each hour, with untrusted samples."""
    rng = random.Random(seed)
    rows = []
    for hour_index in range(4):
        hour = H0 + timedelta(hours=hour_index)
        cursor = 0
        pod = 0
        while cursor < 12:
            length = rng.randint(1, 12 - cursor)
            for t in _steps(hour, cursor, cursor + length):
                source = "custom_rate" if rng.random() < 0.1 else "opencost"
                rows.append(_row(f"api-{hour_index}-{pod}", t, cost=round(rng.uniform(0.5, 2.0), 3), source=source))
            cursor += length + rng.randint(0, 1)
            pod += 1
    raw, compressed = await _buckets_before_and_after(db, rows, 4)
    for before, after in zip(raw, compressed):
        assert after.coverage == pytest.approx(before.coverage, rel=1e-9)
        assert after.replicas == pytest.approx(before.replicas, rel=1e-9)
        assert after.core_hours == pytest.approx(before.core_hours, rel=1e-9)
        if before.cost is not None and after.cost is not None:
            # The trusted share of each pod is extrapolated over its lifetime: exact when the
            # untrusted samples carry the pod's trusted rate, close otherwise.
            assert after.cost_coverage == pytest.approx(before.cost_coverage, abs=0.1)


def _rollup_row(hour: datetime, *, untrusted_samples: int) -> CombinedMetric:
    """One pod-hour as the compressor stores it: 12 samples at 0.001, ``untrusted_samples`` of them custom-rate."""
    trusted = 12 - untrusted_samples
    return CombinedMetric(
        pod_name="api",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        timestamp=hour,
        duration_seconds=12 * STEP,
        sample_count=12,
        first_sample_at=hour,
        last_sample_at=hour + timedelta(seconds=11 * STEP),
        total_cost=0.001 * trusted + 0.5 * untrusted_samples,
        cost_cpu=0.00075 * 12,
        cost_ram=0.00025 * 12,
        cost_source="mixed" if untrusted_samples else "opencost",
        trusted_cost=0.001 * trusted,
        trusted_cost_cpu=0.00075 * trusted,
        trusted_cost_ram=0.00025 * trusted,
        trusted_cost_seconds=float(trusted * STEP),
        usage_source="prometheus",
        cpu_request=500,
        memory_request=512 * 1024**2,
        cpu_usage_millicores=100,
        joules=120.0,
        co2e_grams=12.0,
    )


def test_a_week_with_one_untrusted_sample_in_30_percent_of_hours_keeps_its_coverage():
    """MEA-05 acceptance 2: hourly rows of 7 days, one custom-rate sample in 30% of the hours."""
    rng = random.Random(7)
    hours = [H0 + timedelta(hours=index) for index in range(7 * 24)]
    tainted = set(rng.sample(range(len(hours)), k=round(0.3 * len(hours))))
    rows = [_rollup_row(hour, untrusted_samples=1 if index in tainted else 0) for index, hour in enumerate(hours)]
    buckets = hourly_buckets(rows, TimeWindow(start=hours[0], end=hours[-1] + timedelta(hours=1)), STEP)
    assert len(buckets) == len(hours)
    costed = [bucket for bucket in buckets if bucket.cost is not None and bucket.cost_coverage >= MIN_HOUR_COVERAGE]
    assert len(costed) / len(hours) >= 0.97
    assert sum(bucket.cost_coverage for bucket in buckets) / len(hours) >= 0.97
    assert all(bucket.cost == pytest.approx(0.012) for bucket in costed)


def test_untrusted_cost_hours_count_the_untrusted_samples_of_rollup_hours():
    """MEA-05 rest: one custom-rate sample in 30% of a week's hourly rows is 1/12 h per tainted hour."""
    rng = random.Random(7)
    hours = [H0 + timedelta(hours=index) for index in range(7 * 24)]
    tainted = set(rng.sample(range(len(hours)), k=round(0.3 * len(hours))))
    rows = [_rollup_row(hour, untrusted_samples=1 if index in tainted else 0) for index, hour in enumerate(hours)]
    buckets = hourly_buckets(rows, TimeWindow(start=hours[0], end=hours[-1] + timedelta(hours=1)), STEP)
    assert untrusted_cost_hours(buckets) == pytest.approx(len(tainted) / 12, abs=0.01)
    # A fully untrusted hour counts as one hour; an hour without rows counts nothing.
    rows = [_rollup_row(H0, untrusted_samples=12), _rollup_row(H0 + timedelta(hours=1), untrusted_samples=0)]
    buckets = hourly_buckets(rows, TimeWindow(start=H0, end=H0 + timedelta(hours=3)), STEP)
    assert untrusted_cost_hours(buckets) == pytest.approx(1.0)
    # On an accelerated grid a slot is shorter than an hour.
    assert untrusted_cost_hours(buckets, slot_seconds=600) == pytest.approx(600 / 3600, abs=1e-4)


async def test_a_measurement_read_across_the_compression_watermark_gives_the_raw_buckets(db):
    """ROL-03 acceptance 3: the measurement read (read_combined_metrics_smart) of a window half compressed."""
    rows = []
    for hour_index in range(6):
        hour = H0 + timedelta(hours=hour_index)
        # A rollout in every hour: the old pod until :25, the new one from :25; one custom-rate sample.
        rows += [_row(f"api-{hour_index}-old", t) for t in _steps(hour, 0, 5)]
        rows += [_row(f"api-{hour_index}-new", t) for t in _steps(hour, 5, 11)]
        rows.append(_row(f"api-{hour_index}-new", hour + timedelta(seconds=11 * STEP), source="custom_rate"))
    repo = _repository(db)
    await repo.write_combined_metrics(rows)
    window = TimeWindow(start=H0, end=H0 + timedelta(hours=6))
    raw = hourly_buckets(await repo.read_combined_metrics_smart(window.start, window.end, "prod"), window, STEP)
    compressor = MetricsCompressor(db, Config(), collection_watermark_provider=None, rollup_enabled=False)
    await compressor.run(now=H0 + timedelta(hours=3, days=1))
    assert await compressor.get_watermark("combined_metrics") == H0 + timedelta(hours=3)
    mixed = hourly_buckets(await repo.read_combined_metrics_smart(window.start, window.end, "prod"), window, STEP)
    assert len(raw) == len(mixed) == 6
    assert raw[0].cost == pytest.approx(12.0)
    assert raw[0].cost_coverage == pytest.approx(11 / 12)
    _assert_same(raw, mixed)
