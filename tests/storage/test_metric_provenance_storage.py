"""Storage contract for metric provenance, cost components and intensity freshness.

Covers roadmap A4 (cost components), A7 (container limits), A8 (intensity
staleness, no persisted defaults), A9 (recompute updates container rows) and
A11 (provenance columns, hourly readers keep ``sample_count`` — BUG-29) on
SQLite and PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE
from greenkube.models.metrics import CombinedMetric
from greenkube.models.provenance import is_cost_trusted, is_usage_trusted
from greenkube.storage.base_repository import CombinedMetricsRepository
from greenkube.storage.postgres.repository import PostgresCarbonIntensityRepository, PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCarbonIntensityRepository, SQLiteCombinedMetricsRepository
from greenkube.utils.time import to_iso_z

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")
    monkeypatch.setenv("INTENSITY_MAX_AGE", "6h")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_prov_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "provenance.sqlite"))
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


def _repos(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresCombinedMetricsRepository(manager), PostgresCarbonIntensityRepository(manager)
    return SQLiteCombinedMetricsRepository(manager), SQLiteCarbonIntensityRepository(manager)


def _pod_row(timestamp: datetime, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {
        "pod_name": "redis-0",
        "namespace": "cache",
        "timestamp": timestamp,
        "duration_seconds": 300,
        "total_cost": 0.5,
        "cost_cpu": 0.2,
        "cost_ram": 0.15,
        "cost_storage": 0.1,
        "cost_network": 0.03,
        "cost_lb": 0.02,
        "joules": 3.6e6,
        "pue": 1.0,
        "grid_intensity": 10.0,
        "co2e_grams": 10.0,
        "emaps_zone": "FR",
        "cpu_usage_millicores": 120,
        "memory_usage_bytes": 64 * 1024 * 1024,
        "cost_source": "opencost",
        "usage_source": "prometheus",
        "power_source": "instance_profile",
        "intensity_source": "zone_default",
        "is_estimated": False,
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _container_row(timestamp: datetime, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {"container_name": "redis", "cpu_limit": 500, "memory_limit": 256 * 1024 * 1024}
    values.update(overrides)
    return _pod_row(timestamp, **values)


async def test_pod_and_container_rows_round_trip_provenance_components_and_limits(db):
    repo, _ = _repos(db)
    await repo.write_combined_metrics([_pod_row(T0), _container_row(T0)])

    pods = await repo.read_combined_metrics(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    containers = await repo.read_container_metrics(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))

    for row in (*pods, *containers):
        assert row.cost_source == "opencost"
        assert row.usage_source == "prometheus"
        assert row.power_source == "instance_profile"
        assert row.intensity_source == "zone_default"
        assert row.cost_cpu == pytest.approx(0.2)
        assert row.cost_ram == pytest.approx(0.15)
        assert row.cost_storage == pytest.approx(0.1)
        assert row.cost_network == pytest.approx(0.03)
        assert row.cost_lb == pytest.approx(0.02)
        assert is_cost_trusted(row) and is_usage_trusted(row)
    assert containers[0].cpu_limit == 500
    assert containers[0].memory_limit == 256 * 1024 * 1024
    assert containers[0].emaps_zone == "FR"
    assert containers[0].is_estimated is False


async def test_paginated_readers_return_provenance(db):
    repo, _ = _repos(db)
    await repo.write_combined_metrics([_pod_row(T0)])
    if db.db_type == "postgres":
        _, rows = await repo.read_combined_metrics_page(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    else:
        _, rows, _ = await repo.read_combined_metrics_cursor(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    latest = await repo.read_latest_per_pod(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))
    for row in (*rows, *latest):
        assert row.cost_source == "opencost"
        assert row.cost_cpu == pytest.approx(0.2)


async def test_hourly_readers_keep_sample_count_and_provenance(db):
    """BUG-29: hourly container readers dropped sample_count; provenance must survive compression."""
    repo, _ = _repos(db)
    samples = [T0 + timedelta(minutes=5 * index) for index in range(12)]
    rows = [_pod_row(ts) for ts in samples] + [_container_row(ts) for ts in samples]
    # One sample in the next hour uses a different usage source: that bucket becomes "mixed".
    rows += [
        _container_row(T0 + timedelta(hours=1)),
        _container_row(T0 + timedelta(hours=1, minutes=5), usage_source="metrics_server"),
    ]
    await repo.write_combined_metrics(rows)

    await MetricsCompressor(db, Config()).run(now=T0 + timedelta(days=2))

    # Hourly readers are half-open: [start, end) (A2).
    pod_hourly = await repo.read_hourly_metrics(T0, T0 + timedelta(hours=1))
    container_hourly = await repo.read_hourly_container_metrics(T0, T0 + timedelta(hours=2))

    assert [row.sample_count for row in pod_hourly] == [12]
    by_bucket = {row.timestamp: row for row in container_hourly}
    first = by_bucket[T0]
    assert first.sample_count == 12
    assert first.cost_source == "opencost" and first.usage_source == "prometheus"
    assert first.total_cost == pytest.approx(6.0)
    assert first.cost_cpu == pytest.approx(2.4)
    assert first.cpu_limit == 500
    assert is_cost_trusted(first) and is_usage_trusted(first)
    second = by_bucket[T0 + timedelta(hours=1)]
    assert second.sample_count == 2
    assert second.usage_source == "mixed"
    assert not is_usage_trusted(second)


async def test_intensity_lookup_ignores_values_older_than_max_age(db):
    _, carbon = _repos(db)
    await carbon.save_history(
        [{"carbonIntensity": 42.0, "datetime": (T0 - timedelta(hours=10)).isoformat(), "isEstimated": False}],
        "FR",
    )
    await carbon.save_history(
        [{"carbonIntensity": 55.0, "datetime": (T0 - timedelta(hours=2)).isoformat(), "isEstimated": True}],
        "DE",
    )

    assert await carbon.get_for_zone_at_time("FR", T0.isoformat()) is None
    stale = await carbon.get_intensity_point("FR", T0.isoformat())
    assert stale is not None and stale.value == 42.0
    assert stale.age(T0) == timedelta(hours=10)

    assert await carbon.get_for_zone_at_time("DE", T0.isoformat()) == 55.0
    fresh = await carbon.get_intensity_point("DE", T0.isoformat())
    assert fresh is not None and fresh.is_estimated is True


async def test_default_fallback_records_are_never_persisted(db):
    _, carbon = _repos(db)
    saved = await carbon.save_history(
        [
            {
                "carbonIntensity": 21.0,
                "datetime": T0.isoformat(),
                "isEstimated": True,
                "estimationMethod": "default_fallback",
            }
        ],
        "FR",
    )

    assert saved == 0
    assert await carbon.get_intensity_point("FR", T0.isoformat()) is None


async def test_consolidation_updates_estimation_flags(db):
    _, carbon = _repos(db)
    await carbon.save_history([{"carbonIntensity": 50.0, "datetime": T0.isoformat(), "isEstimated": True}], "FR")
    await carbon.save_history(
        [{"carbonIntensity": 48.0, "datetime": T0.isoformat(), "isEstimated": False, "estimationMethod": "final"}],
        "FR",
    )

    point = await carbon.get_intensity_point("FR", T0.isoformat())
    assert point is not None
    assert point.value == 48.0
    assert point.is_estimated is False


async def test_recompute_updates_pod_and_container_rows(db):
    """BUG-06: container rows must not drift from their pod aggregate after a recompute."""
    repo, carbon = _repos(db)
    sample = T0 + timedelta(minutes=30)
    pod = _pod_row(sample, joules=3.6e6, pue=1.5, grid_intensity=0.0, co2e_grams=0.0, intensity_source="zone_default")
    container = _container_row(
        sample, joules=1.8e6, pue=1.5, grid_intensity=0.0, co2e_grams=0.0, intensity_source="zone_default"
    )
    stale_zone = _pod_row(sample, pod_name="other", emaps_zone="DE", grid_intensity=7.0, co2e_grams=7.0)
    await repo.write_combined_metrics([pod, container, stale_zone])
    await carbon.save_history([{"carbonIntensity": 40.0, "datetime": T0.isoformat(), "isEstimated": True}], "FR")
    # DE only has a value older than INTENSITY_MAX_AGE: rows keep their default.
    await carbon.save_history(
        [{"carbonIntensity": 99.0, "datetime": (T0 - timedelta(hours=12)).isoformat(), "isEstimated": False}], "DE"
    )

    updated = await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1))
    assert updated == 2

    pods = {row.pod_name: row for row in await repo.read_combined_metrics(T0, T0 + timedelta(hours=1))}
    containers = await repo.read_container_metrics(T0, T0 + timedelta(hours=1))
    assert pods["redis-0"].grid_intensity == pytest.approx(40.0)
    assert pods["redis-0"].co2e_grams == pytest.approx(1.5 * 40.0)
    assert pods["redis-0"].intensity_source == "realtime"
    assert containers[0].grid_intensity == pytest.approx(40.0)
    assert containers[0].co2e_grams == pytest.approx(0.5 * 1.5 * 40.0)
    assert containers[0].intensity_source == "realtime"
    assert pods["other"].grid_intensity == pytest.approx(7.0)

    # Consolidation with the same value only upgrades the provenance.
    await carbon.save_history([{"carbonIntensity": 40.0, "datetime": T0.isoformat(), "isEstimated": False}], "FR")
    assert await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1)) == 2
    containers = await repo.read_container_metrics(T0, T0 + timedelta(hours=1))
    assert containers[0].intensity_source == "consolidated"
    assert await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1)) == 0


async def _insert_legacy_default(manager: DatabaseManager, zone: str, at: datetime, value: float) -> None:
    """Store a zone default the way collectors did before A8 (bypassing ``save_history``)."""
    async with manager.connection_scope() as conn:
        if manager.db_type == "postgres":
            await conn.execute(
                "INSERT INTO carbon_intensity_history (zone, carbon_intensity, datetime, is_estimated, "
                "estimation_method) VALUES ($1, $2, $3, TRUE, 'default_fallback')",
                zone,
                value,
                at,
            )
        else:
            await conn.execute(
                "INSERT INTO carbon_intensity_history (zone, carbon_intensity, datetime, is_estimated, "
                "estimation_method) VALUES (?, ?, ?, 1, 'default_fallback')",
                (zone, value, to_iso_z(at)),
            )
            await conn.commit()


async def test_legacy_default_fallback_rows_never_pass_for_measurements(db):
    """BUG-07: zone defaults persisted by older versions are ignored by lookups, the calculator and the recompute."""
    repo, carbon = _repos(db)
    zone_default = float(DEFAULT_GRID_INTENSITY_BY_ZONE["FR"])
    await _insert_legacy_default(db, "FR", T0 - timedelta(hours=1), 21.0)

    assert await carbon.get_for_zone_at_time("FR", T0.isoformat()) is None
    assert await carbon.get_intensity_point("FR", T0.isoformat()) is None

    result = await CarbonCalculator(carbon, pue=1.0).calculate_emissions(3.6e6, "FR", T0 + timedelta(minutes=10))
    assert result is not None
    assert result.intensity_source == "zone_default"
    assert result.grid_intensity == pytest.approx(zone_default)

    sample = T0 + timedelta(minutes=10)
    defaults = {"grid_intensity": zone_default, "co2e_grams": zone_default, "intensity_source": "zone_default"}
    await repo.write_combined_metrics([_pod_row(sample, **defaults), _container_row(sample, **defaults)])

    assert await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1)) == 0
    pods = await repo.read_combined_metrics(T0, T0 + timedelta(hours=1))
    containers = await repo.read_container_metrics(T0, T0 + timedelta(hours=1))
    for row in (*pods, *containers):
        assert row.intensity_source == "zone_default"
        assert row.grid_intensity == pytest.approx(zone_default)


async def test_legacy_default_fallback_row_does_not_hide_an_older_measurement(db):
    repo, carbon = _repos(db)
    await carbon.save_history(
        [{"carbonIntensity": 40.0, "datetime": (T0 - timedelta(hours=2)).isoformat(), "isEstimated": False}], "FR"
    )
    await _insert_legacy_default(db, "FR", T0 - timedelta(hours=1), 21.0)

    assert await carbon.get_for_zone_at_time("FR", T0.isoformat()) == pytest.approx(40.0)
    point = await carbon.get_intensity_point("FR", T0.isoformat())
    assert point is not None and point.value == pytest.approx(40.0) and point.is_estimated is False

    sample = T0 + timedelta(minutes=10)
    await repo.write_combined_metrics(
        [_pod_row(sample, grid_intensity=26.0, co2e_grams=26.0, intensity_source="zone_default")]
    )
    assert await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1)) == 1
    (row,) = await repo.read_combined_metrics(T0, T0 + timedelta(hours=1))
    assert row.grid_intensity == pytest.approx(40.0)
    assert row.intensity_source == "consolidated"


@pytest.mark.parametrize("implementation", ["sql", "python_fallback"])
async def test_recompute_drops_the_intensity_default_reason(db, implementation):
    """A row moved from a default to a measured intensity no longer claims the default in its legacy flags."""
    repo, carbon = _repos(db)
    recompute = (
        repo.recompute_carbon_with_latest_intensities
        if implementation == "sql"
        else partial(CombinedMetricsRepository.recompute_carbon_with_latest_intensities, repo)
    )
    sample = T0 + timedelta(minutes=10)
    default_reason = "Carbon intensity uses the zone default"
    other_reason = "No PUE profile for provider 'aws'. Used default PUE 1.2"
    defaulted: dict[str, Any] = {
        "grid_intensity": 26.0,
        "co2e_grams": 26.0,
        "intensity_source": "zone_default",
        "is_estimated": True,
        "estimation_reasons": [default_reason],
    }
    await repo.write_combined_metrics(
        [
            _pod_row(sample, **defaulted),
            _container_row(sample, **defaulted),
            _pod_row(
                sample, **{**defaulted, "pod_name": "api-0", "estimation_reasons": [other_reason, default_reason]}
            ),
        ]
    )
    await carbon.save_history([{"carbonIntensity": 40.0, "datetime": T0.isoformat(), "isEstimated": True}], "FR")

    assert await recompute(carbon, T0, T0 + timedelta(hours=1)) == 3

    pods = {row.pod_name: row for row in await repo.read_combined_metrics(T0, T0 + timedelta(hours=1))}
    (container,) = await repo.read_container_metrics(T0, T0 + timedelta(hours=1))
    for row in (pods["redis-0"], container):
        assert row.intensity_source == "realtime"
        assert row.grid_intensity == pytest.approx(40.0)
        assert row.estimation_reasons == []
        assert row.is_estimated is False
    assert pods["api-0"].estimation_reasons == [other_reason]
    assert pods["api-0"].is_estimated is True
    assert await recompute(carbon, T0, T0 + timedelta(hours=1)) == 0


async def test_recompute_survives_malformed_legacy_reasons(db):
    repo, carbon = _repos(db)
    sample = T0 + timedelta(minutes=10)
    await repo.write_combined_metrics([_pod_row(sample, intensity_source="zone_default", is_estimated=True)])
    malformed = '["Carbon intensity uses the zone default"'
    async with db.connection_scope() as conn:
        if db.db_type == "postgres":
            await conn.execute("UPDATE combined_metrics SET estimation_reasons = $1", malformed)
        else:
            await conn.execute("UPDATE combined_metrics SET estimation_reasons = ?", (malformed,))
            await conn.commit()
    await carbon.save_history([{"carbonIntensity": 40.0, "datetime": T0.isoformat(), "isEstimated": True}], "FR")

    assert await repo.recompute_carbon_with_latest_intensities(carbon, T0, T0 + timedelta(hours=1)) == 1
    (row,) = await repo.read_combined_metrics(T0, T0 + timedelta(hours=1))
    assert row.intensity_source == "realtime"
    assert row.is_estimated is True


@pytest.mark.parametrize(
    ("sources", "expected", "trusted"),
    [
        (["opencost", "opencost"], "opencost", True),
        (["opencost_derived", "opencost_derived"], "opencost_derived", True),
        (["opencost", "opencost_derived"], "opencost_derived", True),
        (["opencost", "pricing_catalog"], "mixed", False),
        (["opencost_derived", "none"], "mixed", False),
        (["opencost_derived", "custom_rate"], "mixed", False),
        (["opencost_derived", "mixed"], "mixed", False),
        (["opencost", "opencost_derived", None], "mixed", False),
    ],
)
async def test_hourly_cost_source_keeps_opencost_and_derived_rows_trusted(db, sources, expected, trusted):
    """OpenCost rounds small pods below its precision; an hour mixing both OpenCost labels stays trusted."""
    repo, _ = _repos(db)
    stamps = [T0 + timedelta(minutes=5 * index) for index in range(len(sources))]
    rows = [_container_row(ts, cost_source=source) for ts, source in zip(stamps, sources, strict=True)]
    rows += [_pod_row(ts, cost_source=source) for ts, source in zip(stamps, sources, strict=True)]
    await repo.write_combined_metrics(rows)

    await MetricsCompressor(db, Config()).run(now=T0 + timedelta(days=2))

    (pod,) = await repo.read_hourly_metrics(T0, T0 + timedelta(hours=1))
    (container,) = await repo.read_hourly_container_metrics(T0, T0 + timedelta(hours=1))
    for row in (pod, container):
        assert row.cost_source == expected
        assert is_cost_trusted(row) is trusted
