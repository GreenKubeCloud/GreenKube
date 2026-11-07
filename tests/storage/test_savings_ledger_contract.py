"""Contract tests for the append-only savings ledger on SQLite and PostgreSQL.

Covers roadmap C0.4, C0.6, C0.7 and C0.14 (BUG-30, BUG-32, BUG-33, BUG-40).
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import SavingsLedgerRecord
from greenkube.storage.base_savings_repository import AccrualWatermarkConflict
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository

CLUSTER = "contract-cluster"
HOUR = timedelta(hours=1)
YEAR_SECONDS = 365.25 * 24 * 3600


@dataclass
class LedgerDatabase:
    backend: str
    manager: DatabaseManager
    repo: SQLiteSavingsLedgerRepository | PostgresSavingsLedgerRepository

    async def scalar(self, sql: str):
        async with self.manager.connection_scope() as conn:
            if self.backend == "postgres":
                return await conn.fetchval(sql)  # type: ignore[union-attr]
            cursor = await conn.execute(sql)
            row = await cursor.fetchone()
            return row[0] if row else None


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def ledger_db(request, tmp_path, monkeypatch) -> AsyncGenerator[LedgerDatabase, None]:
    backend = request.param
    postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    schema = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if backend == "postgres":
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL savings ledger contract tests.")
        schema = f"gk_ledger_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    repo = PostgresSavingsLedgerRepository(manager) if backend == "postgres" else SQLiteSavingsLedgerRepository(manager)
    try:
        yield LedgerDatabase(backend=backend, manager=manager, repo=repo)
    finally:
        await manager.close()
        if schema and postgres_dsn:
            conn = await asyncpg.connect(postgres_dsn)
            try:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await conn.close()


def _applied(
    rec_id: int = 1,
    *,
    applied_at: datetime,
    co2e: float = YEAR_SECONDS / 3600,  # 1 g per hour
    cost: float = YEAR_SECONDS / 3600 / 100,  # 0.01 $ per hour
    namespace: str = "prod",
    status: RecommendationStatus = RecommendationStatus.APPLIED,
    **updates,
) -> RecommendationRecord:
    return RecommendationRecord(
        id=rec_id,
        namespace=namespace,
        pod_name=f"pod-{rec_id}",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=status,
        applied_at=applied_at,
        carbon_saved_co2e_grams=co2e,
        cost_saved=cost,
        potential_savings_co2e_grams=co2e,
        potential_savings_cost=cost,
        verification_status="pending",
        **updates,
    )


async def _row_count(db: LedgerDatabase) -> int:
    return int(await db.scalar("SELECT COUNT(*) FROM recommendation_savings_ledger") or 0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_two_attribution_runs_write_one_row_per_slot(ledger_db: LedgerDatabase):
    """BUG-33: repeated or overlapping runs never double count a slot."""
    now = datetime(2026, 10, 1, 12, 7, tzinfo=timezone.utc)
    record = _applied(applied_at=now - timedelta(hours=3))
    attributor = SavingsAttributor(ledger_db.repo, CLUSTER)

    first = await attributor.accrue([record], now=now)
    second = await attributor.accrue([record], now=now + timedelta(seconds=2))
    third = await attributor.accrue([record], now=now + timedelta(minutes=5))

    assert first.written == 3  # 09:07→10:00 (partial), 10:00→11:00, 11:00→12:00
    assert second.written == 0 and third.written == 0
    assert await _row_count(ledger_db) == 3
    totals = await ledger_db.repo.get_cumulative_totals(CLUSTER, group_by_method=True)
    assert totals["projected"]["co2e_saved_grams"] == pytest.approx(53 / 60 + 2, rel=1e-6)


@pytest.mark.asyncio
@pytest.mark.database
async def test_downtime_is_backfilled_exactly_once(ledger_db: LedgerDatabase):
    """BUG-33: slots missed while the controller was down are accrued on restart."""
    start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    record = _applied(applied_at=start)
    attributor = SavingsAttributor(ledger_db.repo, CLUSTER)

    await attributor.accrue([record], now=start + timedelta(hours=2, minutes=1))
    # Controller down for 5 hours, then two runs a few seconds apart.
    await attributor.accrue([record], now=start + timedelta(hours=7, minutes=3))
    await attributor.accrue([record], now=start + timedelta(hours=7, minutes=3, seconds=4))

    assert await _row_count(ledger_db) == 7
    assert await ledger_db.repo.get_accrual_watermark(CLUSTER) == start + timedelta(hours=7)
    totals = await ledger_db.repo.get_cumulative_totals(CLUSTER)
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(7.0, rel=1e-6)


@pytest.mark.asyncio
@pytest.mark.database
async def test_concurrent_writer_cannot_advance_a_stale_watermark(ledger_db: LedgerDatabase):
    """BUG-33: a second controller that read the same watermark writes nothing."""
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        SavingsLedgerRecord(
            recommendation_id=1,
            cluster_name=CLUSTER,
            recommendation_type="RIGHTSIZING_CPU",
            co2e_saved_grams=1.0,
            period_start=now - HOUR,
            period_end=now,
        )
    ]
    await ledger_db.repo.append_accruals(
        rows, cluster_name=CLUSTER, stream="s", expected_watermark=None, new_watermark=now
    )
    with pytest.raises(AccrualWatermarkConflict):
        await ledger_db.repo.append_accruals(
            [r.model_copy(update={"recommendation_id": 2}) for r in rows],
            cluster_name=CLUSTER,
            stream="s",
            expected_watermark=None,
            new_watermark=now,
        )
    with pytest.raises(AccrualWatermarkConflict):
        await ledger_db.repo.append_accruals(
            [r.model_copy(update={"recommendation_id": 3}) for r in rows],
            cluster_name=CLUSTER,
            stream="s",
            expected_watermark=now - HOUR,
            new_watermark=now + HOUR,
        )
    assert await _row_count(ledger_db) == 1


@pytest.mark.asyncio
@pytest.mark.database
async def test_save_records_is_append_only(ledger_db: LedgerDatabase):
    """The first accrual of a slot is final; replays never overwrite it."""
    slot = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    record = SavingsLedgerRecord(
        recommendation_id=1,
        cluster_name=CLUSTER,
        recommendation_type="RIGHTSIZING_CPU",
        co2e_saved_grams=10.0,
        period_start=slot,
        period_end=slot + HOUR,
    )
    assert record.slot_start == slot
    assert await ledger_db.repo.save_records([record]) == 1
    assert await ledger_db.repo.save_records([record.model_copy(update={"co2e_saved_grams": 12.0})]) == 0
    totals = await ledger_db.repo.get_cumulative_totals(CLUSTER)
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(10.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_window_includes_partial_first_hour(ledger_db: LedgerDatabase):
    """BUG-40: a window starting mid-slot includes the overlapping share of that slot."""
    base = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    await ledger_db.repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=1,
                cluster_name=CLUSTER,
                namespace="prod",
                recommendation_type="RIGHTSIZING_CPU",
                co2e_saved_grams=6.0,
                cost_saved_dollars=0.6,
                period_start=base + i * HOUR,
                period_end=base + (i + 1) * HOUR,
            )
            for i in range(3)
        ]
    )
    window = await ledger_db.repo.get_window_totals(
        CLUSTER, base + timedelta(minutes=30), base + timedelta(hours=2, minutes=10), namespace="prod"
    )
    # 30 min of slot 1 + slot 2 + 10 min of slot 3
    assert window["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(3.0 + 6.0 + 1.0)
    assert window["RIGHTSIZING_CPU"]["cost_saved_dollars"] == pytest.approx(0.3 + 0.6 + 0.1)
    assert await ledger_db.repo.get_window_totals(CLUSTER, base, base + 3 * HOUR, namespace="dev") == {}


@pytest.mark.asyncio
@pytest.mark.database
async def test_breakdown_keeps_methods_apart(ledger_db: LedgerDatabase):
    """C0.4: projected, measured and user-declared accruals are never merged."""
    slot = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    rows = [
        SavingsLedgerRecord(
            recommendation_id=rec_id,
            cluster_name=CLUSTER,
            namespace="prod",
            recommendation_type="RIGHTSIZING_CPU",
            co2e_saved_grams=value,
            period_start=slot,
            period_end=slot + HOUR,
            measurement_method=method,
        )
        for rec_id, method, value in ((1, "prorated", 1.0), (2, "measured", 2.0), (3, "user_declared", -4.0))
    ]
    await ledger_db.repo.save_records(rows)
    breakdown = await ledger_db.repo.get_accrual_breakdown(CLUSTER, slot, slot + HOUR)
    by_method = {row.method: row.co2e_saved_grams for row in breakdown}
    assert by_method == {"projected": pytest.approx(1.0), "measured": 2.0, "user_declared": -4.0}


@pytest.mark.asyncio
@pytest.mark.database
async def test_lifetime_counters_are_monotonic(ledger_db: LedgerDatabase):
    """C0.14: superseding accruals increases the reversal counter, never decreases accruals."""
    slot = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    await ledger_db.repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=rec_id,
                cluster_name=CLUSTER,
                recommendation_type="RIGHTSIZING_CPU",
                co2e_saved_grams=value,
                cost_saved_dollars=value / 10,
                period_start=slot,
                period_end=slot + HOUR,
            )
            for rec_id, value in ((1, 5.0), (2, 3.0), (3, -1.0))
        ]
    )
    before = (await ledger_db.repo.get_lifetime_counters(CLUSTER))[0]
    assert (before.accrued_co2e_grams, before.reversed_co2e_grams) == pytest.approx((8.0, 1.0))

    assert await ledger_db.repo.supersede_for_recommendation(1) == 1
    assert await ledger_db.repo.supersede_for_recommendation(3) == 1
    after = (await ledger_db.repo.get_lifetime_counters(CLUSTER))[0]
    assert after.accrued_co2e_grams >= before.accrued_co2e_grams
    assert after.reversed_co2e_grams >= before.reversed_co2e_grams
    net = after.accrued_co2e_grams - after.reversed_co2e_grams
    assert net == pytest.approx(3.0)
    totals = await ledger_db.repo.get_cumulative_totals(CLUSTER)
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(net)


@pytest.mark.asyncio
@pytest.mark.database
async def test_hourly_rollup_view_conserves_sums(ledger_db: LedgerDatabase):
    """C0.6: the read-side hourly rollup aggregates raw rows without losing any."""
    hour = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    await ledger_db.repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=1,
                cluster_name=CLUSTER,
                recommendation_type="RIGHTSIZING_CPU",
                co2e_saved_grams=1.0,
                period_seconds=300,
                timestamp=hour + timedelta(minutes=5 * (i + 1)),
            )
            for i in range(12)
        ]
    )
    assert await ledger_db.scalar("SELECT COUNT(*) FROM savings_ledger_hourly_rollup") == 1
    assert await ledger_db.scalar("SELECT SUM(co2e_saved_grams) FROM savings_ledger_hourly_rollup") == pytest.approx(
        12.0
    )
    assert await ledger_db.scalar("SELECT SUM(entry_count) FROM savings_ledger_hourly_rollup") == 12


@pytest.mark.asyncio
@pytest.mark.database
async def test_accrual_floor_prevents_reaccruing_existing_rows(ledger_db: LedgerDatabase):
    """Rows written before the grid existed (wall-clock periods) are never accrued again."""
    now = datetime(2026, 10, 1, 12, 5, tzinfo=timezone.utc)
    legacy_end = datetime(2026, 10, 1, 11, 37, 12, tzinfo=timezone.utc)
    await ledger_db.repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=1,
                cluster_name=CLUSTER,
                namespace="prod",
                recommendation_type="RIGHTSIZING_CPU",
                co2e_saved_grams=1.0,
                period_seconds=300,
                timestamp=legacy_end,
            )
        ]
    )
    record = _applied(applied_at=now - timedelta(days=2))
    result = await SavingsAttributor(ledger_db.repo, CLUSTER).accrue([record], now=now)

    assert result.written == 1  # only 11:37:12 → 12:00
    totals = await ledger_db.repo.get_cumulative_totals(CLUSTER)
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(1.0 + (22 * 60 + 48) / 3600, rel=1e-6)


@pytest.mark.asyncio
@pytest.mark.database
async def test_namespace_filters_select_namespaced_and_cluster_scoped_rows(ledger_db: LedgerDatabase):
    slot = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    await ledger_db.repo.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=rec_id,
                cluster_name=CLUSTER,
                namespace=namespace,
                recommendation_type=rec_type,
                co2e_saved_grams=value,
                cost_saved_dollars=value / 10,
                period_start=slot,
                period_end=slot + HOUR,
            )
            for rec_id, namespace, rec_type, value in (
                (1, "prod", "RIGHTSIZING_CPU", 10.0),
                (2, "dev", "RIGHTSIZING_CPU", 20.0),
                (3, "", "OVERPROVISIONED_NODE", 5.0),
                (4, "prod", "RIGHTSIZING_MEMORY", 7.0),
            )
        ]
    )
    window = (slot, slot + HOUR)
    assert await ledger_db.repo.get_window_totals(CLUSTER, *window, namespace="prod") == {
        "RIGHTSIZING_CPU": {"co2e_saved_grams": pytest.approx(10.0), "cost_saved_dollars": pytest.approx(1.0)},
        "RIGHTSIZING_MEMORY": {"co2e_saved_grams": pytest.approx(7.0), "cost_saved_dollars": pytest.approx(0.7)},
    }
    assert await ledger_db.repo.get_window_totals(CLUSTER, *window, namespace="") == {
        "OVERPROVISIONED_NODE": {"co2e_saved_grams": pytest.approx(5.0), "cost_saved_dollars": pytest.approx(0.5)}
    }
    everything = await ledger_db.repo.get_window_totals(CLUSTER, *window)
    assert everything["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(30.0)
    assert await ledger_db.repo.get_window_totals("other-cluster", *window) == {}
