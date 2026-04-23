# tests/core/test_metrics_compressor.py
"""
Tests for MetricsCompressor (15% coverage → target ≥ 80%).

MetricsCompressor is critical for production data management:
- Compresses raw 5-minute combined_metrics into hourly aggregates.
- Prunes old raw rows to prevent unbounded DB growth / OOM on large datasets.
- Must not raise even if a DB operation fails (graceful degradation).
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.core.metrics_compressor import MetricsCompressor
from greenkube.models.metrics import CombinedMetric

# ---------------------------------------------------------------------------
# Fixture: real in-memory SQLite DB with the full schema
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Yield a fully initialised in-memory SQLite DatabaseManager."""
    manager = DatabaseManager()
    await manager.setup_sqlite(db_path=":memory:")
    yield manager
    await manager.close()


@pytest.fixture
def compressor(db):
    return MetricsCompressor(db_manager=db)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hours_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _metric(pod: str = "pod-a", ns: str = "ns-1", hours_ago: int = 25) -> CombinedMetric:
    """Return a CombinedMetric old enough to be compressed by default config."""
    return CombinedMetric(
        pod_name=pod,
        namespace=ns,
        total_cost=0.01,
        co2e_grams=5.0,
        joules=1_000.0,
        timestamp=_ts(hours_ago),
        pue=1.2,
        grid_intensity=100.0,
    )


async def _insert_raw(db, metrics):
    """Write metrics directly via the SQLite repository."""
    from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

    repo = SQLiteCombinedMetricsRepository(db)
    await repo.write_combined_metrics(metrics)


async def _count_raw(db) -> int:
    async with db.connection_scope() as conn:
        async with conn.execute("SELECT COUNT(*) FROM combined_metrics") as cur:
            row = await cur.fetchone()
            return row[0]


async def _count_hourly(db) -> int:
    async with db.connection_scope() as conn:
        async with conn.execute("SELECT COUNT(*) FROM combined_metrics_hourly") as cur:
            row = await cur.fetchone()
            return row[0]


# ---------------------------------------------------------------------------
# run() — top-level stats dict
# ---------------------------------------------------------------------------


class TestMetricsCompressorRun:
    """run() returns a stats dict and never raises."""

    @pytest.mark.asyncio
    async def test_run_returns_stats_dict(self, compressor):
        """run() always returns a dict with the four expected keys."""
        stats = await compressor.run()

        assert isinstance(stats, dict)
        for key in ("hours_compressed", "rows_compressed", "raw_rows_pruned", "hourly_rows_pruned"):
            assert key in stats

    @pytest.mark.asyncio
    async def test_run_on_empty_db_returns_zero_counts(self, compressor):
        """Running on an empty database produces all-zero stats."""
        stats = await compressor.run()

        assert stats["hours_compressed"] == 0
        assert stats["rows_compressed"] == 0
        assert stats["raw_rows_pruned"] == 0
        assert stats["hourly_rows_pruned"] == 0

    @pytest.mark.asyncio
    async def test_run_does_not_raise_on_db_error(self, db):
        """run() catches internal errors and returns stats without re-raising."""
        from unittest.mock import MagicMock

        bad_db = MagicMock()
        bad_db.db_type = "sqlite"

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _bad_scope():
            raise RuntimeError("simulated DB failure")
            yield  # noqa: unreachable

        bad_db.connection_scope = _bad_scope
        compressor = MetricsCompressor(db_manager=bad_db)

        # Must not raise
        stats = await compressor.run()
        assert isinstance(stats, dict)


# ---------------------------------------------------------------------------
# _compress_sqlite — raw → hourly aggregation
# ---------------------------------------------------------------------------


class TestSQLiteCompression:
    """_compress_sqlite aggregates raw rows into combined_metrics_hourly."""

    @pytest.mark.asyncio
    async def test_compress_creates_hourly_row(self, db, compressor):
        """Old raw metrics produce at least one row in combined_metrics_hourly."""
        metrics = [_metric(hours_ago=30), _metric(pod="pod-b", hours_ago=30)]
        await _insert_raw(db, metrics)

        stats = await compressor.run()

        assert await _count_hourly(db) >= 1
        assert stats["hours_compressed"] >= 1

    @pytest.mark.asyncio
    async def test_recent_metrics_not_compressed(self, db, compressor):
        """Metrics younger than METRICS_COMPRESSION_AGE_HOURS are NOT compressed."""
        metrics = [_metric(hours_ago=1)]  # 1 hour old → recent
        await _insert_raw(db, metrics)

        await compressor.run()

        # Row should still be in raw table, not moved to hourly
        assert await _count_raw(db) == 1
        assert await _count_hourly(db) == 0

    @pytest.mark.asyncio
    async def test_compression_aggregates_same_pod_same_hour(self, db, compressor):
        """Two raw metrics for the same pod in the same hour produce ONE hourly row."""
        base_ts = datetime.now(timezone.utc) - timedelta(hours=30)
        m1 = CombinedMetric(
            pod_name="agg-pod",
            namespace="ns",
            total_cost=0.10,
            co2e_grams=10.0,
            joules=2_000.0,
            timestamp=base_ts.replace(minute=0),
            pue=1.2,
            grid_intensity=100.0,
        )
        m2 = CombinedMetric(
            pod_name="agg-pod",
            namespace="ns",
            total_cost=0.20,
            co2e_grams=20.0,
            joules=4_000.0,
            timestamp=base_ts.replace(minute=30),
            pue=1.2,
            grid_intensity=100.0,
        )
        await _insert_raw(db, [m1, m2])

        await compressor.run()

        assert await _count_hourly(db) == 1  # Aggregated into a single hour bucket

    @pytest.mark.asyncio
    async def test_hourly_row_sums_joules(self, db, compressor):
        """The hourly row contains the SUM of joules from the raw rows."""
        base_ts = datetime.now(timezone.utc) - timedelta(hours=30)
        m1 = CombinedMetric(
            pod_name="sum-pod",
            namespace="ns",
            total_cost=0.01,
            co2e_grams=5.0,
            joules=1_000.0,
            timestamp=base_ts.replace(minute=5),
            pue=1.2,
            grid_intensity=100.0,
        )
        m2 = CombinedMetric(
            pod_name="sum-pod",
            namespace="ns",
            total_cost=0.01,
            co2e_grams=5.0,
            joules=3_000.0,
            timestamp=base_ts.replace(minute=10),
            pue=1.2,
            grid_intensity=100.0,
        )
        await _insert_raw(db, [m1, m2])
        await compressor.run()

        async with db.connection_scope() as conn:
            async with conn.execute("SELECT joules FROM combined_metrics_hourly WHERE pod_name = 'sum-pod'") as cur:
                row = await cur.fetchone()

        assert row is not None
        assert row[0] == pytest.approx(4_000.0)

    @pytest.mark.asyncio
    async def test_idempotent_recompression(self, db, compressor):
        """Running compression twice does not duplicate hourly rows."""
        metrics = [_metric(hours_ago=30)]
        await _insert_raw(db, metrics)

        await compressor.run()
        await compressor.run()  # Second run should upsert, not duplicate

        assert await _count_hourly(db) == 1


# ---------------------------------------------------------------------------
# _prune_raw — retention policy for raw rows
# ---------------------------------------------------------------------------


class TestSQLiteRawPruning:
    """_prune_raw deletes old raw rows based on METRICS_RAW_RETENTION_DAYS."""

    @pytest.mark.asyncio
    async def test_prune_removes_old_raw_rows(self, db):
        """Rows older than retention_days are deleted from combined_metrics."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.METRICS_RAW_RETENTION_DAYS = 7  # 7-day retention
        compressor = MetricsCompressor(db_manager=db, config=cfg)

        old_metric = _metric(hours_ago=8 * 24)  # 8 days old
        recent_metric = _metric(pod="pod-b", hours_ago=1)
        await _insert_raw(db, [old_metric, recent_metric])

        stats = await compressor.run()

        assert stats["raw_rows_pruned"] == 1
        assert await _count_raw(db) == 1  # Only the recent one remains

    @pytest.mark.asyncio
    async def test_prune_disabled_when_retention_is_negative(self, db):
        """METRICS_RAW_RETENTION_DAYS = -1 disables pruning."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.METRICS_RAW_RETENTION_DAYS = -1
        compressor = MetricsCompressor(db_manager=db, config=cfg)

        await _insert_raw(db, [_metric(hours_ago=365 * 24)])  # Very old

        stats = await compressor.run()

        assert stats["raw_rows_pruned"] == 0
        assert await _count_raw(db) == 1  # Row was NOT deleted


# ---------------------------------------------------------------------------
# _prune_hourly — retention policy for hourly aggregates
# ---------------------------------------------------------------------------


class TestSQLiteHourlyPruning:
    """_prune_hourly deletes old hourly rows based on METRICS_AGGREGATED_RETENTION_DAYS."""

    @pytest.mark.asyncio
    async def test_prune_hourly_removes_old_aggregates(self, db):
        """Hourly rows older than METRICS_AGGREGATED_RETENTION_DAYS are deleted."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.METRICS_COMPRESSION_AGE_HOURS = 1  # Compress anything > 1h old
        cfg.METRICS_RAW_RETENTION_DAYS = -1  # Don't prune raw
        cfg.METRICS_AGGREGATED_RETENTION_DAYS = 30  # Prune hourly older than 30 days
        compressor = MetricsCompressor(db_manager=db, config=cfg)

        # Insert and compress a row that is 60 days old
        old_metric = _metric(hours_ago=60 * 24)
        await _insert_raw(db, [old_metric])
        # First compress it into the hourly table
        await compressor._compress_to_hourly()

        assert await _count_hourly(db) == 1

        # Now prune — the hourly row is 60 days old, beyond 30-day retention
        pruned = await compressor._prune_hourly()

        assert pruned == 1
        assert await _count_hourly(db) == 0

    @pytest.mark.asyncio
    async def test_prune_hourly_disabled_when_negative(self, db):
        """METRICS_AGGREGATED_RETENTION_DAYS = -1 disables hourly pruning."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.METRICS_COMPRESSION_AGE_HOURS = 1
        cfg.METRICS_RAW_RETENTION_DAYS = -1
        cfg.METRICS_AGGREGATED_RETENTION_DAYS = -1
        compressor = MetricsCompressor(db_manager=db, config=cfg)

        old_metric = _metric(hours_ago=60 * 24)
        await _insert_raw(db, [old_metric])
        await compressor._compress_to_hourly()

        pruned = await compressor._prune_hourly()

        assert pruned == 0
        assert await _count_hourly(db) == 1


# ---------------------------------------------------------------------------
# refresh_namespace_cache
# ---------------------------------------------------------------------------


class TestRefreshNamespaceCache:
    """refresh_namespace_cache updates namespace_cache from recent raw metrics."""

    @pytest.mark.asyncio
    async def test_refresh_populates_namespace_cache(self, db, compressor):
        """After writing metrics and refreshing, namespace_cache has the namespace."""
        recent = _metric(hours_ago=1)
        await _insert_raw(db, [recent])

        count = await compressor.refresh_namespace_cache()

        assert count >= 1

        async with db.connection_scope() as conn:
            async with conn.execute("SELECT namespace FROM namespace_cache") as cur:
                rows = await cur.fetchall()
        namespaces = {r[0] for r in rows}
        assert "ns-1" in namespaces
