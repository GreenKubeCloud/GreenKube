"""Contract tests for the append-only savings ledger v2 on SQLite and PostgreSQL (roadmap C6, N9, BUG-39).

Covers INSERT-only enforcement, the per-cluster hash chain and its tamper
detection, projection from the v1 ledger (dual write), supersession,
restatement of projected accruals by measured ones, drift reversals, the
reconciliation of the accrued book with the v1 totals and the cluster identity.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, List, Optional
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import METHOD_MEASURED, SavingsLedgerRecord
from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.verify.ledger.identity import (
    ClusterIdentityResolver,
    ClusterIdentityUnavailable,
    forget_cached_identities,
)
from greenkube.verify.ledger.model import (
    BOOK_ACCRUED,
    BOOK_RESTATEMENT,
    ENTRY_ACCRUAL,
    ENTRY_RESTATEMENT,
    ENTRY_REVERSAL,
    METHOD_MEASURED_ALLOCATED,
    METHOD_PROJECTED,
    QUANTITY_CO2E,
    QUANTITY_COST,
    REASON_DRIFT,
    REASON_RESTATEMENT,
    REASON_SUPERSESSION,
    LedgerEntryDraft,
)
from greenkube.verify.ledger.projector import LedgerProjector
from greenkube.verify.ledger.verification import CHAIN_VERIFICATION_FAILURES, verify_chain, verify_ledger

CLUSTER = "ledger-v2-cluster"
CLUSTER_UID = "0b5c1d9e-7f6a-4d0e-9a51-kube-system"
HOUR = timedelta(hours=1)
YEAR_SECONDS = 365.25 * 24 * 3600


@dataclass
class LedgerDb:
    backend: str
    manager: DatabaseManager
    v1: SQLiteSavingsLedgerRepository | PostgresSavingsLedgerRepository
    repo: LedgerRepository

    async def execute(self, sql: str) -> None:
        async with self.manager.connection_scope() as conn:
            if self.backend == "postgres":
                await conn.execute(sql)  # type: ignore[union-attr]
            else:
                await conn.execute(sql)
                await conn.commit()

    async def scalar(self, sql: str):
        async with self.manager.connection_scope() as conn:
            if self.backend == "postgres":
                return await conn.fetchval(sql)  # type: ignore[union-attr]
            cursor = await conn.execute(sql)
            row = await cursor.fetchone()
            return row[0] if row else None


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def ledger(request, tmp_path, monkeypatch) -> AsyncGenerator[LedgerDb, None]:
    backend = request.param
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    schema = None
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if backend == "postgres":
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL savings ledger v2 contract tests.")
        schema = f"gk_ledger_v2_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    v1 = PostgresSavingsLedgerRepository(manager) if backend == "postgres" else SQLiteSavingsLedgerRepository(manager)
    forget_cached_identities()
    try:
        yield LedgerDb(backend=backend, manager=manager, v1=v1, repo=LedgerRepository(manager))
    finally:
        forget_cached_identities()
        await manager.close()
        if schema and dsn:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await conn.close()


def _hour(offset_hours: int = 0) -> datetime:
    """A slot-aligned instant ``offset_hours`` from the current UTC hour (never a fixed calendar date)."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now + timedelta(hours=offset_hours)


def _record(
    rec_id: int,
    *,
    applied_at: datetime,
    cost_per_hour: float = 0.01,
    co2e_per_hour: float = 1.0,
    status: RecommendationStatus = RecommendationStatus.APPLIED,
    namespace: str = "prod",
    **updates,
) -> RecommendationRecord:
    hours_per_year = YEAR_SECONDS / 3600
    updates.setdefault("verification_status", "pending")
    return RecommendationRecord(
        id=rec_id,
        namespace=namespace,
        pod_name=f"pod-{rec_id}",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=status,
        applied_at=applied_at,
        carbon_saved_co2e_grams=co2e_per_hour * hours_per_year,
        cost_saved=cost_per_hour * hours_per_year,
        potential_savings_co2e_grams=co2e_per_hour * hours_per_year,
        potential_savings_cost=cost_per_hour * hours_per_year,
        **updates,
    )


async def _uid() -> Optional[str]:
    return CLUSTER_UID


def _projector(db: LedgerDb, records: Optional[List[RecommendationRecord]] = None, reader=_uid) -> LedgerProjector:
    async def provider():
        return list(records or [])

    resolver = ClusterIdentityResolver(db.repo, CLUSTER, reader=reader)
    return LedgerProjector(db.repo, resolver, records=provider, currency="EUR")


def _draft(key: str, value: float = 1.0) -> LedgerEntryDraft:
    start = _hour(-1)
    return LedgerEntryDraft(
        entry_key=key,
        group_key=key,
        entry_kind=ENTRY_ACCRUAL,
        book=BOOK_ACCRUED,
        reason="accrual",
        method=METHOD_PROJECTED,
        quantity=QUANTITY_COST,
        value=value,
        unit="EUR",
        currency="EUR",
        method_version="test/1",
        period_start=start,
        period_end=start + HOUR,
        created_by="test",
        recommendation_id=1,
        recommendation_ids=[1],
    )


async def _net(db: LedgerDb, *, method: str, quantity: str, book: Optional[str] = None) -> float:
    rows = await db.repo.summary(CLUSTER_UID)
    return math.fsum(
        row.value
        for row in rows
        if row.method == method and row.quantity == quantity and (book is None or row.book == book)
    )


async def _v1_totals(db: LedgerDb, start=None, end=None) -> dict:
    breakdown = await db.v1.get_accrual_breakdown(CLUSTER, start, end)
    totals: dict = {}
    for row in breakdown:
        bucket = totals.setdefault(row.method, {"cost": 0.0, "co2e": 0.0})
        bucket["cost"] += row.cost_saved_dollars
        bucket["co2e"] += row.co2e_saved_grams
    return totals


# ----------------------------------------------------------------------
# INSERT-only enforcement
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_update_delete_are_rejected_by_the_database(ledger: LedgerDb):
    inserted = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("k1")])
    assert len(inserted) == 1
    entry_id = inserted[0].entry_id
    for statement in (
        f"UPDATE savings_ledger_entries SET value = 99 WHERE entry_id = '{entry_id}'",
        f"DELETE FROM savings_ledger_entries WHERE entry_id = '{entry_id}'",
    ):
        with pytest.raises(Exception, match="append-only"):
            await ledger.execute(statement)
    if ledger.backend == "postgres":
        with pytest.raises(Exception, match="append-only"):
            await ledger.execute("TRUNCATE savings_ledger_entries")
    stored = await ledger.repo.get_entry(entry_id)
    assert stored is not None and stored.value == 1.0


@pytest.mark.asyncio
@pytest.mark.database
async def test_appends_are_idempotent_and_chained_without_gaps(ledger: LedgerDb):
    first = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("a"), _draft("b"), _draft("a")])
    again = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("b"), _draft("c")])
    assert [entry.seq for entry in first] == [1, 2]
    assert [entry.seq for entry in again] == [3]
    assert again[0].prev_hash == first[-1].hash
    # Another cluster has its own chain.
    other = await ledger.repo.append("other-uid", "other", [_draft("a")])
    assert other[0].seq == 1 and other[0].prev_hash is None
    result = await verify_chain(ledger.repo, CLUSTER_UID)
    assert result.ok and result.entries == 3


# ----------------------------------------------------------------------
# Dual write, chain verification and tamper detection
# ----------------------------------------------------------------------


async def _accrue_realistic(db: LedgerDb, hours: int = 48) -> List[RecommendationRecord]:
    """Three recommendations accrued hourly for two days through the real attribution path."""
    start = _hour(-hours)
    records = [
        _record(1, applied_at=start + timedelta(minutes=17), cost_per_hour=0.0123, co2e_per_hour=2.5),
        _record(2, applied_at=start + timedelta(hours=5), cost_per_hour=0.5, co2e_per_hour=0.0, namespace="batch"),
        # A signed (negative) projection: never floored.
        _record(3, applied_at=start + timedelta(hours=7, minutes=42), cost_per_hour=-0.02, co2e_per_hour=-0.75),
    ]
    attributor = SavingsAttributor(db.v1, CLUSTER, initial_backfill=timedelta(hours=hours), max_slots_per_run=hours)
    result = await attributor.accrue(records, now=_hour(0) + timedelta(minutes=3))
    assert result.written > 0
    return records


@pytest.mark.asyncio
@pytest.mark.database
async def test_projection_reconciles_with_v1_and_chain_detects_tampering(ledger: LedgerDb):
    await _accrue_realistic(ledger)
    projector = _projector(ledger)
    sync = await projector.sync()
    assert sync.cluster_uid == CLUSTER_UID and sync.projected > 0
    assert (await projector.sync()).written == 0  # idempotent

    v1 = await _v1_totals(ledger)
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST) == pytest.approx(
        v1["projected"]["cost"], rel=1e-12
    )
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_CO2E) == pytest.approx(
        v1["projected"]["co2e"], rel=1e-12
    )
    # Signed values are kept: recommendation 3 accrues negative amounts.
    negatives = await ledger.repo.list_entries(CLUSTER_UID, filters={"recommendation_id": 3}, limit=500)
    assert negatives and all(entry.value < 0 for entry in negatives)
    # A slot-aligned window reconciles too.
    window = (_hour(-20), _hour(-4))
    windowed = await ledger.repo.summary(CLUSTER_UID, start=window[0], end=window[1])
    v1_window = await _v1_totals(ledger, *window)
    assert math.fsum(row.value for row in windowed if row.quantity == QUANTITY_COST) == pytest.approx(
        v1_window["projected"]["cost"], rel=1e-9
    )

    checkpoints: dict = {}
    results = await verify_ledger(ledger.repo, checkpoints=checkpoints)
    assert results[CLUSTER_UID].ok
    head_seq = results[CLUSTER_UID].head_seq
    assert head_seq == await ledger.repo.count(CLUSTER_UID)

    # Tamper with one value behind the triggers' back.
    victim = (await ledger.repo.list_entries(CLUSTER_UID, limit=1, ascending=True, before_seq=40))[0]
    if ledger.backend == "postgres":
        await ledger.execute(
            "ALTER TABLE savings_ledger_entries DISABLE TRIGGER trg_savings_ledger_entries_append_only"
        )
        await ledger.execute(
            f"UPDATE savings_ledger_entries SET value = value * 2 WHERE entry_id = '{victim.entry_id}'"
        )
    else:
        await ledger.execute("DROP TRIGGER trg_savings_ledger_entries_no_update")
        await ledger.execute(
            f"UPDATE savings_ledger_entries SET value = value * 2 WHERE entry_id = '{victim.entry_id}'"
        )
    before = CHAIN_VERIFICATION_FAILURES.labels(reason="hash_mismatch")._value.get()
    # The incremental pass re-checks its anchor, then a full pass finds the modified row.
    results = await verify_ledger(ledger.repo, checkpoints=checkpoints, full=True)
    failed = results[CLUSTER_UID]
    assert not failed.ok and failed.reason == "hash_mismatch" and failed.failure_seq == victim.seq
    assert CHAIN_VERIFICATION_FAILURES.labels(reason="hash_mismatch")._value.get() == before + 1
    status = await ledger.repo.get_chain_status(CLUSTER_UID)
    assert status is not None and not status.ok and "hash_mismatch" in (status.failure or "")


@pytest.mark.asyncio
@pytest.mark.database
async def test_verification_detects_a_removed_tail(ledger: LedgerDb):
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(5)])
    assert (await verify_ledger(ledger.repo, checkpoints={}))[CLUSTER_UID].ok
    if ledger.backend == "postgres":
        await ledger.execute(
            "ALTER TABLE savings_ledger_entries DISABLE TRIGGER trg_savings_ledger_entries_append_only"
        )
    else:
        await ledger.execute("DROP TRIGGER trg_savings_ledger_entries_no_delete")
    await ledger.execute("DELETE FROM savings_ledger_entries WHERE seq = 5")
    result = (await verify_ledger(ledger.repo, checkpoints={}))[CLUSTER_UID]
    assert not result.ok and result.reason == "truncated"


# ----------------------------------------------------------------------
# Supersession, restatement (BUG-39) and drift reversals
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_superseded_v1_rows_are_reversed_and_accrued_book_still_reconciles(ledger: LedgerDb):
    await _accrue_realistic(ledger)
    projector = _projector(ledger)
    await projector.sync()
    await ledger.v1.supersede_for_recommendation(2)
    sync = await projector.sync()
    assert sync.superseded > 0
    reversals = await ledger.repo.list_entries(
        CLUSTER_UID, filters={"recommendation_id": 2, "entry_kind": ENTRY_REVERSAL}, limit=500
    )
    assert reversals and all(entry.reason == REASON_SUPERSESSION and entry.reverses_entry_id for entry in reversals)
    rec2 = await ledger.repo.list_entries(CLUSTER_UID, filters={"recommendation_id": 2}, limit=500)
    assert math.fsum(entry.value for entry in rec2) == pytest.approx(0.0, abs=1e-12)
    v1 = await _v1_totals(ledger)
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST, book=BOOK_ACCRUED) == pytest.approx(
        v1["projected"]["cost"], rel=1e-12
    )
    assert (await projector.sync()).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_measurement_restates_projected_accruals_with_linked_entries(ledger: LedgerDb):
    applied_at = _hour(-30) + timedelta(minutes=20)
    verified_at = _hour(-10) + timedelta(minutes=30)
    pending = _record(7, applied_at=applied_at, cost_per_hour=0.04, co2e_per_hour=3.0)
    attributor = SavingsAttributor(ledger.v1, CLUSTER, initial_backfill=timedelta(hours=40), max_slots_per_run=48)
    # The change accrues its projection until the measurement concludes ...
    await attributor.accrue([pending], now=verified_at - timedelta(minutes=1))
    hours_per_year = YEAR_SECONDS / 3600
    verified = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": verified_at,
            "verification_status": "passed",
            "measured_cost_saved": 0.03 * hours_per_year,
            "measured_co2e_saved_grams": -0.5 * hours_per_year,
        }
    )
    # ... then its measured saving (the slot holding verified_at is split).
    await attributor.accrue([verified], now=_hour(0) + timedelta(minutes=2))
    projector = _projector(ledger, records=[verified])
    sync = await projector.sync()
    assert sync.restated > 0

    restatements = await ledger.repo.list_entries(
        CLUSTER_UID, filters={"entry_kind": ENTRY_RESTATEMENT}, limit=1000, ascending=True
    )
    assert restatements
    for entry in restatements:
        assert entry.method == METHOD_MEASURED_ALLOCATED and entry.book == BOOK_RESTATEMENT
        projection = await ledger.repo.get_entry(entry.restates_entry_id or "")
        assert projection is not None and projection.method == METHOD_PROJECTED
        assert projection.period_end <= verified_at
        reversal = await ledger.repo.list_entries(
            CLUSTER_UID,
            filters={"entry_kind": ENTRY_REVERSAL, "reason": REASON_RESTATEMENT},
            limit=1000,
        )
        assert any(
            item.reverses_entry_id == projection.entry_id and item.value == -projection.value for item in reversal
        )
        hours = (entry.period_end - entry.period_start).total_seconds() / 3600
        expected = 0.03 * hours if entry.quantity == QUANTITY_COST else -0.5 * hours
        assert entry.value == pytest.approx(expected, rel=1e-9)  # signed, never floored

    # Restated: nothing stays projected before verification, measured covers the whole life.
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST) == pytest.approx(0.0, abs=1e-12)
    life_hours = (_hour(0) - applied_at).total_seconds() / 3600
    assert await _net(ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST) == pytest.approx(
        0.03 * life_hours, rel=1e-9
    )
    # The accrued book still matches the v1 ledger.
    v1 = await _v1_totals(ledger)
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST, book=BOOK_ACCRUED) == pytest.approx(
        v1["projected"]["cost"], rel=1e-12
    )
    assert await _net(
        ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST, book=BOOK_ACCRUED
    ) == pytest.approx(v1[METHOD_MEASURED]["cost"], rel=1e-12)
    assert (await projector.sync()).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok

    # A later supersession voids both books of the recommendation.
    await ledger.v1.supersede_for_recommendation(7)
    await projector.sync()
    for method in (METHOD_PROJECTED, METHOD_MEASURED_ALLOCATED):
        assert await _net(ledger, method=method, quantity=QUANTITY_COST) == pytest.approx(0.0, abs=1e-12)
        assert await _net(ledger, method=method, quantity=QUANTITY_COST, book=BOOK_ACCRUED) == pytest.approx(
            0.0, abs=1e-12
        )
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_drift_reversal_becomes_a_linked_reversal_entry(ledger: LedgerDb):
    verified_at = _hour(-20)
    hours_per_year = YEAR_SECONDS / 3600
    record = _record(
        9,
        applied_at=_hour(-24),
        status=RecommendationStatus.VERIFIED,
        verified_at=verified_at,
        verification_status="passed",
        measured_cost_saved=0.1 * hours_per_year,
        measured_co2e_saved_grams=0.0,
    )
    attributor = SavingsAttributor(ledger.v1, CLUSTER, initial_backfill=timedelta(hours=30), max_slots_per_run=48)
    await attributor.accrue([record], now=_hour(0) + timedelta(minutes=1))
    onset = _hour(-6) + timedelta(minutes=10)
    floor = _hour(0)
    seconds = (floor - onset).total_seconds()
    await ledger.v1.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=9,
                cluster_name=CLUSTER,
                namespace="prod",
                recommendation_type="RIGHTSIZING_CPU",
                cost_saved_dollars=-0.1 * hours_per_year * seconds / YEAR_SECONDS,
                co2e_saved_grams=0.0,
                period_seconds=int(seconds),
                timestamp=floor,
                period_start=onset,
                period_end=floor,
                measurement_method=METHOD_MEASURED,
            )
        ]
    )
    await _projector(ledger).sync()
    drift = await ledger.repo.list_entries(CLUSTER_UID, filters={"reason": REASON_DRIFT}, limit=10)
    assert len(drift) == 1
    entry = drift[0]
    assert entry.entry_kind == ENTRY_REVERSAL and entry.method == METHOD_MEASURED_ALLOCATED
    assert entry.value == pytest.approx(-0.1 * seconds / 3600, rel=1e-9)
    target = await ledger.repo.get_entry(entry.reverses_entry_id or "")
    assert target is not None and target.period_start <= onset < target.period_end
    v1 = await _v1_totals(ledger)
    assert await _net(ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST) == pytest.approx(
        v1[METHOD_MEASURED]["cost"], rel=1e-12
    )


# ----------------------------------------------------------------------
# Gap-free projection (v1 ids committed out of order)
# ----------------------------------------------------------------------


def _hourly_rows(count: int, *, rec_id: int = 1, cost: float = 0.01, co2e: float = 0.5) -> List[SavingsLedgerRecord]:
    first = _hour(-count - 1)
    return [
        SavingsLedgerRecord(
            recommendation_id=rec_id,
            cluster_name=CLUSTER,
            namespace="prod",
            recommendation_type="RIGHTSIZING_CPU",
            cost_saved_dollars=cost,
            co2e_saved_grams=co2e,
            period_seconds=3600,
            timestamp=first + timedelta(hours=index + 1),
            period_start=first + timedelta(hours=index),
            period_end=first + timedelta(hours=index + 1),
            slot_start=first + timedelta(hours=index),
        )
        for index in range(count)
    ]


async def _assert_reconciles(db: LedgerDb) -> None:
    v1 = await _v1_totals(db)
    assert await _net(db, method=METHOD_PROJECTED, quantity=QUANTITY_COST) == pytest.approx(
        v1["projected"]["cost"], rel=1e-12
    )
    assert await _net(db, method=METHOD_PROJECTED, quantity=QUANTITY_CO2E) == pytest.approx(
        v1["projected"]["co2e"], rel=1e-12
    )


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_v1_row_visible_after_higher_ids_were_projected_is_never_lost(ledger: LedgerDb, monkeypatch):
    """A row that becomes visible far below the watermark is found by the anti-join sweep."""
    from greenkube.verify.ledger import projector as projector_module

    monkeypatch.setattr(projector_module, "WATERMARK_LOOKBACK", 5)
    await ledger.v1.save_records(_hourly_rows(30))
    # The first row is not visible to the projector yet (as if its transaction had not committed).
    await ledger.execute(
        "UPDATE recommendation_savings_ledger SET cluster_name = 'not-yet' "
        "WHERE id = (SELECT MIN(id) FROM recommendation_savings_ledger)"
    )
    projector = _projector(ledger)
    assert (await projector.sync()).projected == 29 * 2
    await ledger.execute(
        "UPDATE recommendation_savings_ledger SET cluster_name = '" + CLUSTER + "' WHERE cluster_name = 'not-yet'"
    )
    assert await ledger.repo.count_unprojected_v1_rows("recommendation_savings_ledger", CLUSTER, CLUSTER_UID) == 1

    late = projector_module.LEDGER_LATE_ROWS.labels(source_table="recommendation_savings_ledger")
    before = late._value.get()
    result = await projector.sync(full=True)
    assert result.projected == 2
    assert late._value.get() == before + 1
    gauge = projector_module.LEDGER_UNPROJECTED_ROWS.labels(source_table="recommendation_savings_ledger")
    assert gauge._value.get() == 0
    await _assert_reconciles(ledger)
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_postgres_out_of_order_commit_of_a_large_batch_is_fully_projected(ledger: LedgerDb):
    """An uncommitted batch of more than WATERMARK_LOOKBACK ids, and a later id committed and projected first."""
    if ledger.backend != "postgres":
        pytest.skip("Concurrent transactions on the v1 sequence only exist on PostgreSQL.")
    from greenkube.verify.ledger.projector import WATERMARK_LOOKBACK

    batch = _hourly_rows(WATERMARK_LOOKBACK + 440, rec_id=1)
    straggler = _hourly_rows(1, rec_id=2, cost=0.25, co2e=3.0)
    projector = _projector(ledger)
    async with ledger.manager.connection_scope() as conn:
        transaction = conn.transaction()  # type: ignore[union-attr]
        await transaction.start()
        try:
            await ledger.v1._insert(conn, batch)  # type: ignore[union-attr]
            # Another writer (the drift monitor) commits a higher id; a pass projects it.
            await ledger.v1.save_records(straggler)
            assert (await projector.sync()).projected == 2
        except BaseException:
            await transaction.rollback()
            raise
        await transaction.commit()
    # Incremental passes (the attribution job's) and the ledger-chain loop's full pass.
    await projector.sync()
    await projector.sync()
    await projector.sync(full=True)
    total = await ledger.scalar("SELECT COUNT(*) FROM savings_ledger_entries")
    assert total == 2 * (len(batch) + 1)
    assert await ledger.repo.count_unprojected_v1_rows("recommendation_savings_ledger", CLUSTER, CLUSTER_UID) == 0
    await _assert_reconciles(ledger)
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


# ----------------------------------------------------------------------
# Cluster identity (N9)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_cluster_identity_is_stored_once_and_transient_errors_write_nothing(ledger: LedgerDb):
    async def unavailable():
        raise ClusterIdentityUnavailable("api server unreachable")

    await _accrue_realistic(ledger, hours=4)
    skipped = await _projector(ledger, reader=unavailable).sync()
    assert skipped.skipped == "cluster_identity_unavailable" and skipped.written == 0
    assert await ledger.repo.count(CLUSTER_UID) == 0

    first = await ClusterIdentityResolver(ledger.repo, CLUSTER, reader=_uid).resolve()
    assert first.cluster_uid == CLUSTER_UID and first.source == "kube-system"

    async def other():
        return "another-uid"

    again = await ClusterIdentityResolver(ledger.repo, CLUSTER, reader=other).resolve()
    assert again.cluster_uid == CLUSTER_UID  # first resolution wins: the chain key never changes

    async def no_cluster():
        return None

    local = await ClusterIdentityResolver(ledger.repo, "laptop", reader=no_cluster).resolve()
    assert local.cluster_uid == "name:laptop" and local.source == "cluster_name"


@pytest.mark.asyncio
@pytest.mark.database
async def test_change_rows_carry_the_cluster_uid_once_resolved(ledger: LedgerDb, monkeypatch):
    from greenkube.storage.change_repository import ChangeRepository
    from tests.verify.fixtures import change_event  # pyrefly: ignore[missing-import]

    monkeypatch.setattr("greenkube.core.savings_attributor.savings_cluster_name", lambda: CLUSTER)
    changes = ChangeRepository(ledger.manager)
    await changes.insert_change(change_event(_hour(-30), change_id="change-before-identity"))
    await ClusterIdentityResolver(ledger.repo, CLUSTER, reader=_uid).resolve()
    after = change_event(_hour(-20), change_id="change-after-identity").model_copy(update={"workload_uid": "uid-2"})
    await changes.insert_change(after)
    before_uid = await ledger.scalar("SELECT cluster_uid FROM change_events WHERE change_id = 'change-before-identity'")
    after_uid = await ledger.scalar("SELECT cluster_uid FROM change_events WHERE change_id = 'change-after-identity'")
    assert before_uid is None and after_uid == CLUSTER_UID
