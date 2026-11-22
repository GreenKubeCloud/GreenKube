"""The pre-computed dashboard summary keeps the node idle share and cluster totals (WP-05.3, NRG-02).

The Footprint page reads the summary cache for its fixed windows: the cached
cluster-wide rows carry the idle share and the cluster totals with the rule of
``/metrics/summary`` (unknown, never zero, when the idle history does not
cover the window); namespace rows never carry them.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.summary_refresher import SummaryRefresher
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.postgres.summary_repository import PostgresSummaryRepository
from greenkube.storage.postgres.timeseries_cache_repository import PostgresTimeseriesCacheRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.storage.sqlite.summary_repository import SQLiteSummaryRepository
from greenkube.storage.sqlite.timeseries_cache_repository import SQLiteTimeseriesCacheRepository

# "Now" of the refresher is pinned (the ``clock`` fixture) so the cached windows never depend on
# the time of day the suite runs at: just after midnight UTC, just after an hour boundary, mid-hour.
CLOCK_TIMES = {
    "after-midnight": datetime(2026, 10, 10, 0, 0, 30, tzinfo=timezone.utc),
    "after-hour": datetime(2026, 10, 10, 13, 0, 20, tzinfo=timezone.utc),
    "mid-hour": datetime(2026, 10, 10, 13, 31, 0, tzinfo=timezone.utc),
    "late-hour": datetime(2026, 10, 10, 13, 58, 40, tzinfo=timezone.utc),
}
NOW = CLOCK_TIMES["mid-hour"]
START = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
POD_JOULES = 150.0 * 300
IDLE_JOULES = 450.0 * 300


@pytest.fixture(autouse=True, params=list(CLOCK_TIMES), ids=list(CLOCK_TIMES))
def clock(request, monkeypatch) -> datetime:
    """Pin "now" of the summary refresher and the seeded hours to one of :data:`CLOCK_TIMES`."""
    import greenkube.core.summary_refresher as refresher_module

    now = CLOCK_TIMES[request.param]
    module = __import__(__name__, fromlist=["NOW"])
    monkeypatch.setattr(module, "NOW", now)
    monkeypatch.setattr(module, "START", now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(refresher_module, "datetime", FrozenDateTime)
    return now


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL summary tests.")
        schema = f"gk_sumidle_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "summary-idle.sqlite"))
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


def _repos(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return (
            PostgresCombinedMetricsRepository(manager),
            PostgresSummaryRepository(manager),
            PostgresTimeseriesCacheRepository(manager),
        )
    return (
        SQLiteCombinedMetricsRepository(manager),
        SQLiteSummaryRepository(manager),
        SQLiteTimeseriesCacheRepository(manager),
    )


def _pod(step: datetime) -> CombinedMetric:
    return CombinedMetric(
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


def _idle(step: datetime) -> NodeIdleMetric:
    return NodeIdleMetric(
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


async def test_cached_cluster_rows_carry_the_idle_share_and_cluster_totals(db):
    metrics, summary, cache = _repos(db)
    steps = [START + timedelta(minutes=5 * i) for i in range(12)]
    await metrics.write_combined_metrics([_pod(step) for step in steps])
    await metrics.write_idle_metrics([_idle(step) for step in steps])

    await SummaryRefresher(metrics, summary, cache, namespaces=["prod"]).run()

    rows = {row.window_slug: row for row in await summary.get_rows(namespace=None)}
    day = rows["24h"]
    assert day.total_energy_joules == pytest.approx(12 * POD_JOULES)
    assert day.idle_energy_joules == pytest.approx(12 * IDLE_JOULES)
    assert day.idle_co2e_grams == pytest.approx(36.0)
    assert day.idle_embodied_co2e_grams == pytest.approx(3.6)
    assert day.idle_cost == pytest.approx(0.36)
    assert day.cluster_total_energy_joules == pytest.approx(600.0 * 3600, rel=1e-6)
    assert day.cluster_total_co2e_grams == pytest.approx(48.0)
    assert day.cluster_total_embodied_co2e_grams == pytest.approx(4.8)
    assert day.cluster_total_cost == pytest.approx(0.48)
    assert day.idle_data_since is not None

    namespace_rows = await summary.get_rows(namespace="prod")
    assert namespace_rows
    for row in namespace_rows:
        assert row.idle_energy_joules is None
        assert row.cluster_total_energy_joules is None


async def test_cached_rows_leave_the_idle_share_unknown_when_the_idle_history_is_short(db):
    metrics, summary, cache = _repos(db)
    steps = [START + timedelta(minutes=5 * i) for i in range(12)]
    await metrics.write_combined_metrics([_pod(step) for step in steps])
    # Idle rows only for the last half of the pod rows: the 24h window holds earlier pod rows.
    await metrics.write_idle_metrics([_idle(step) for step in steps[6:]])

    await SummaryRefresher(metrics, summary, cache, namespaces=[]).run()

    day = {row.window_slug: row for row in await summary.get_rows(namespace=None)}["24h"]
    assert day.total_energy_joules == pytest.approx(12 * POD_JOULES)
    assert day.idle_energy_joules is None
    assert day.cluster_total_energy_joules is None
    assert day.cluster_total_cost is None
    assert day.idle_data_since is not None


async def test_cached_rows_without_idle_rows_leave_the_idle_share_unknown(db):
    metrics, summary, cache = _repos(db)
    await metrics.write_combined_metrics([_pod(START)])

    await SummaryRefresher(metrics, summary, cache, namespaces=[]).run()

    day = {row.window_slug: row for row in await summary.get_rows(namespace=None)}["24h"]
    assert day.idle_energy_joules is None
    assert day.cluster_total_energy_joules is None
    assert day.idle_data_since is None
