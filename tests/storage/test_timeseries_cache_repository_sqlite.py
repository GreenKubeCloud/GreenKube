# tests/storage/test_timeseries_cache_repository_sqlite.py
"""Tests for SQLiteTimeseriesCacheRepository."""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.models.metrics import TimeseriesCachePoint
from greenkube.storage.sqlite.timeseries_cache_repository import SQLiteTimeseriesCacheRepository

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS metrics_timeseries_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    window_slug TEXT NOT NULL,
    namespace   TEXT,
    bucket_ts   TEXT NOT NULL,
    co2e_grams          REAL NOT NULL DEFAULT 0,
    embodied_co2e_grams REAL NOT NULL DEFAULT 0,
    total_cost          REAL NOT NULL DEFAULT 0,
    joules              REAL NOT NULL DEFAULT 0,
    UNIQUE(window_slug, namespace, bucket_ts)
);
"""


@pytest.fixture
async def db_conn():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute(CREATE_TABLE)
        await conn.commit()
        yield conn


@pytest.fixture
def repo(db_conn):
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_conn

    db_manager.connection_scope = scope
    return SQLiteTimeseriesCacheRepository(db_manager=db_manager)


def _point(
    window_slug="24h", namespace=None, bucket_ts="2025-01-01T00:00:00", co2e=10.0, embodied=1.0, cost=0.5, joules=5000.0
):
    return TimeseriesCachePoint(
        window_slug=window_slug,
        namespace=namespace,
        bucket_ts=bucket_ts,
        co2e_grams=co2e,
        embodied_co2e_grams=embodied,
        total_cost=cost,
        joules=joules,
    )


class TestUpsertPoints:
    @pytest.mark.asyncio
    async def test_empty_list_is_noop(self, repo):
        await repo.upsert_points([])
        results = await repo.get_points("24h")
        assert results == []

    @pytest.mark.asyncio
    async def test_inserts_points(self, repo):
        points = [_point(bucket_ts=f"2025-01-01T0{i}:00:00") for i in range(3)]
        await repo.upsert_points(points)
        results = await repo.get_points("24h")
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_replaces_existing_points_for_window(self, repo):
        """upsert_points deletes old rows for that window+namespace before inserting."""
        old = [_point(bucket_ts="2025-01-01T00:00:00", co2e=50.0)]
        await repo.upsert_points(old)

        new = [_point(bucket_ts="2025-01-01T01:00:00", co2e=99.0)]
        await repo.upsert_points(new)

        results = await repo.get_points("24h")
        assert len(results) == 1
        assert results[0].co2e_grams == pytest.approx(99.0)

    @pytest.mark.asyncio
    async def test_different_windows_are_independent(self, repo):
        await repo.upsert_points([_point(window_slug="24h", bucket_ts="2025-01-01T00:00:00")])
        await repo.upsert_points([_point(window_slug="7d", bucket_ts="2025-01-01T00:00:00")])

        assert len(await repo.get_points("24h")) == 1
        assert len(await repo.get_points("7d")) == 1

    @pytest.mark.asyncio
    async def test_namespace_points_isolated(self, repo):
        await repo.upsert_points([_point(namespace="prod", bucket_ts="2025-01-01T00:00:00")])
        await repo.upsert_points([_point(namespace="staging", bucket_ts="2025-01-01T00:00:00")])

        prod = await repo.get_points("24h", namespace="prod")
        staging = await repo.get_points("24h", namespace="staging")
        assert len(prod) == 1
        assert len(staging) == 1


class TestGetPoints:
    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self, repo):
        results = await repo.get_points("24h")
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_correct_fields(self, repo):
        await repo.upsert_points([_point(co2e=42.0, cost=3.14, joules=12345.0)])
        r = (await repo.get_points("24h"))[0]
        assert r.window_slug == "24h"
        assert r.co2e_grams == pytest.approx(42.0)
        assert r.total_cost == pytest.approx(3.14)
        assert r.joules == pytest.approx(12345.0)

    @pytest.mark.asyncio
    async def test_results_ordered_by_bucket_ts(self, repo):
        buckets = ["2025-01-01T02:00:00", "2025-01-01T00:00:00", "2025-01-01T01:00:00"]
        await repo.upsert_points([_point(bucket_ts=b) for b in buckets])
        results = await repo.get_points("24h")
        ts_list = [r.bucket_ts for r in results]
        assert ts_list == sorted(ts_list)

    @pytest.mark.asyncio
    async def test_namespace_filter_cluster_only(self, repo):
        """get_points with namespace=None returns only cluster-wide rows."""
        await repo.upsert_points([_point(namespace=None, bucket_ts="2025-01-01T00:00:00")])
        await repo.upsert_points([_point(namespace="prod", bucket_ts="2025-01-01T00:00:00")])

        cluster = await repo.get_points("24h", namespace=None)
        assert len(cluster) == 1
        assert cluster[0].namespace is None
