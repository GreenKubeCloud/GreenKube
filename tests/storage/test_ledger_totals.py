"""Materialized savings ledger totals (LED-09) on SQLite and PostgreSQL.

All-time reads (summary, report breakdown, lifetime counters, accrued book)
come from ``savings_ledger_totals``, maintained in the append transaction,
instead of scanning every entry. They must equal a fold of the entries.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import pytest

from greenkube.verify.ledger.model import BOOK_ACCRUED
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER_UID,
    LedgerDb,
    _accrue_realistic,
    _projector,
    ledger,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "src" / "greenkube" / "core" / "migrations" / "scripts"


async def _entries(db: LedgerDb):
    return await db.repo.list_entries(CLUSTER_UID, limit=100_000, ascending=True)


async def _assert_totals_match_entries(db: LedgerDb) -> None:
    entries = await _entries(db)
    assert entries
    folded: dict = defaultdict(float)
    for entry in entries:
        folded[(entry.method, entry.quantity, entry.book, entry.entry_kind)] += entry.value
    summary: dict = defaultdict(float)
    for row in await db.repo.summary(CLUSTER_UID):
        summary[(row.method, row.quantity, row.book, row.entry_kind)] += row.value
    assert set(summary) == set(folded)
    for key, value in folded.items():
        assert summary[key] == pytest.approx(value, rel=1e-9, abs=1e-12)

    breakdown = await db.repo.accrual_breakdown(CLUSTER_UID)
    expected_cost = math.fsum(e.value for e in entries if e.quantity == "cost" and "cost_of_fix:" not in e.group_key)
    assert math.fsum(row.cost_saved_dollars for row in breakdown) == pytest.approx(expected_cost, rel=1e-9)
    counters = await db.repo.lifetime_counters(CLUSTER_UID)
    positive = math.fsum(e.value for e in entries if e.quantity == "cost" and e.value > 0)
    assert math.fsum(c.accrued_cost_dollars for c in counters) == pytest.approx(positive, rel=1e-9)
    book = await db.repo.accrued_book_totals(CLUSTER_UID)
    accrued = math.fsum(e.value for e in entries if e.quantity == "cost" and e.book == BOOK_ACCRUED)
    assert math.fsum(v for (_, q), v in book.items() if q == "cost") == pytest.approx(accrued, rel=1e-9)


@pytest.mark.asyncio
@pytest.mark.database
async def test_all_time_reads_come_from_the_materialized_totals(ledger: LedgerDb):  # noqa: F811
    await _accrue_realistic(ledger)
    await _projector(ledger).sync()
    groups = await ledger.scalar("SELECT COUNT(*) FROM savings_ledger_totals")
    assert 0 < groups < await ledger.repo.count(CLUSTER_UID)
    await _assert_totals_match_entries(ledger)


@pytest.mark.asyncio
@pytest.mark.database
async def test_upgrade_backfills_totals_from_entries_written_before_them(ledger: LedgerDb):  # noqa: F811
    """Entries written by the release before the totals table are counted once by the migration's backfill."""
    await _accrue_realistic(ledger)
    await _projector(ledger).sync()
    await ledger.execute("DELETE FROM savings_ledger_totals")
    dialect = "postgres" if ledger.backend == "postgres" else "sqlite"
    script = (MIGRATIONS / dialect / "0292_ledger_totals.sql").read_text()
    backfill = script[script.index("INSERT INTO savings_ledger_totals") :].split(";")[0]
    await ledger.execute(backfill)
    await _assert_totals_match_entries(ledger)
