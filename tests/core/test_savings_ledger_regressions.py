"""Regression tests ported from the ledger audit scripts.

- ``ledger_compress.py``: the hourly compression overwrote partially compressed
  buckets (BUG-32) and two attribution runs a few seconds apart double counted
  the same period (BUG-33).
- ``sim_ledger.py``: attribution every 5 minutes with an hourly compression job
  lost about half of the savings (BUG-32).

Both run the real controller jobs (``greenkube.cli.start``) against a real
SQLite database with a fake clock.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import greenkube.cli.start as start_module
import greenkube.core.savings_attributor as attributor_module
import greenkube.storage.sqlite.savings_repository as sqlite_savings_module
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import SavingsLedgerRecord
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository

YEAR_SECONDS = 365.25 * 24 * 3600
ONE_GRAM_PER_HOUR = YEAR_SECONDS / 3600


class _Clock:
    now: datetime = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _FakeDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return _Clock.now


@pytest.fixture
async def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("CLUSTER_NAME", "audit")
    from greenkube.core.config import config

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    repo = SQLiteSavingsLedgerRepository(manager)
    monkeypatch.setattr(attributor_module, "datetime", _FakeDateTime)
    monkeypatch.setattr(sqlite_savings_module, "datetime", _FakeDateTime)
    try:
        yield manager, repo
    finally:
        await manager.close()


async def _scalar(manager: DatabaseManager, sql: str):
    async with manager.connection_scope() as conn:
        cursor = await conn.execute(sql)
        row = await cursor.fetchone()
        assert row is not None
        return row[0]


async def _run_compression_job(repo) -> None:
    compressor = MagicMock()
    compressor.run = AsyncMock(return_value={"hours_compressed": 0, "raw_rows_pruned": 0, "hourly_rows_pruned": 0})
    compressor.refresh_namespace_cache = AsyncMock()
    with (
        patch("greenkube.core.metrics_compressor.MetricsCompressor", return_value=compressor),
        patch("greenkube.core.db.get_db_manager", return_value=MagicMock()),
        patch("greenkube.core.factory.get_savings_ledger_repository", return_value=repo),
    ):
        await start_module.compress_metrics()


async def _run_attribution_job(repo, applied) -> None:
    reco_repo = MagicMock()
    reco_repo.get_applied_recommendations = AsyncMock(return_value=applied)
    from greenkube.verify.ledger import projector as projector_module

    build = projector_module.build_projector

    async def no_records():
        return []

    with (
        patch("greenkube.core.factory.get_recommendation_repository", return_value=reco_repo),
        patch("greenkube.core.factory.get_savings_ledger_repository", return_value=repo),
        # The dual write goes to the test database, never to the process-wide one (left open otherwise).
        patch.object(projector_module, "build_projector", lambda: build(db_manager=repo._db, records=no_records)),
    ):
        await start_module.attribute_recommendation_savings()


def _applied(applied_at: datetime) -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        namespace="ns",
        pod_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=RecommendationStatus.APPLIED,
        verification_status="pending",
        applied_at=applied_at,
        carbon_saved_co2e_grams=ONE_GRAM_PER_HOUR,
        cost_saved=ONE_GRAM_PER_HOUR / 100,
    )


@pytest.mark.asyncio
async def test_compression_job_never_loses_ledger_rows(ledger):
    """ledger_compress.py: a cutoff in the middle of an hour used to drop the first part of the bucket."""
    manager, repo = ledger
    hour = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
    await repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=1,
                cluster_name="audit",
                namespace="ns",
                recommendation_type="RIGHTSIZING_CPU",
                co2e_saved_grams=1.0,
                cost_saved_dollars=1.0,
                period_seconds=300,
                timestamp=hour + timedelta(minutes=5 * (i + 1)),
            )
            for i in range(12)
        ]
    )
    # First run: the 24h cutoff falls 32 minutes into the bucket. Second run one hour later.
    _Clock.now = hour + timedelta(hours=24, minutes=32)
    await _run_compression_job(repo)
    _Clock.now = hour + timedelta(hours=25, minutes=32)
    await _run_compression_job(repo)

    totals = await repo.get_cumulative_totals("audit")
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(12.0)
    assert await _scalar(manager, "SELECT COUNT(*) FROM recommendation_savings_ledger") == 12


@pytest.mark.asyncio
async def test_two_attribution_runs_seconds_apart_accrue_each_slot_once(ledger):
    """ledger_compress.py: two runs 2 s apart used to write two overlapping wall-clock periods."""
    manager, repo = ledger
    applied_at = datetime(2026, 10, 1, 9, 20, tzinfo=timezone.utc)
    applied = [_applied(applied_at)]

    _Clock.now = datetime(2026, 10, 1, 12, 3, tzinfo=timezone.utc)
    await _run_attribution_job(repo, applied)
    _Clock.now += timedelta(seconds=2)
    await _run_attribution_job(repo, applied)

    watermark = await repo.get_accrual_watermark("audit")
    assert watermark == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    expected = (watermark - applied_at).total_seconds() / 3600
    totals = await repo.get_cumulative_totals("audit")
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(expected, rel=1e-9)
    assert await _scalar(manager, "SELECT COUNT(*) FROM recommendation_savings_ledger") == 3


@pytest.mark.asyncio
async def test_attribution_with_hourly_compression_conserves_savings(ledger):
    """sim_ledger.py: 5-minute attribution plus hourly compression must not lose savings."""
    manager, repo = ledger
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    applied = [_applied(t0)]
    compression_starts = t0 + timedelta(days=1, hours=2, minutes=37)

    t = t0 + timedelta(minutes=5)
    end = compression_starts + timedelta(hours=10)
    while t < end:
        _Clock.now = t
        await _run_attribution_job(repo, applied)
        if t >= compression_starts and t.minute == 35:
            _Clock.now = t + timedelta(minutes=2)
            await _run_compression_job(repo)
        t += timedelta(minutes=5)

    watermark = await repo.get_accrual_watermark("audit")
    hours = (watermark - t0).total_seconds() / 3600
    assert hours == 36
    totals = await repo.get_cumulative_totals("audit")
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(hours, rel=1e-9)
    assert await _scalar(manager, "SELECT COUNT(*) FROM recommendation_savings_ledger") == hours
    assert await _scalar(manager, "SELECT COUNT(*) FROM recommendation_savings_ledger_hourly") == 0
