"""Savings ledger WP-09: one accounting basis for projected and measured savings (LED-01).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Optional

import pytest

from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.core.savings_report import realization_summary
from greenkube.models.metrics import RecommendationStatus
from greenkube.verify.ledger.model import (
    BASIS_ALLOCATED,
    BASIS_BILLABLE_EXPECTED,
    BOOK_ACCRUED,
    METHOD_MEASURED_ALLOCATED,
    METHOD_PROJECTED,
    QUANTITY_COST,
)
from greenkube.verify.ledger.verification import verify_chain
from tests.storage import test_ledger_v2_contract as _contract  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]
    CLUSTER,
    CLUSTER_UID,
    YEAR_SECONDS,
    LedgerDb,
    _draft,
    _hour,
    _net,
    _projector,
    _record,
)

ledger = _contract.ledger  # the SQLite and PostgreSQL fixture
HOURS_PER_YEAR = YEAR_SECONDS / 3600
#: 0.01 per hour on the allocated basis.
ALLOCATED_ANNUAL = 0.01 * HOURS_PER_YEAR


def _allocated_record(rec_id: int, *, applied_hours_ago: int, realization: float, **updates):
    """A pending change whose projection is 0.01/h allocated and allocated x realization billable."""
    billable_per_hour = 0.01 * realization
    impact = {
        "allocated_cost_annual": ALLOCATED_ANNUAL,
        "realization_factor": realization,
        "expected_billable_cost_annual": billable_per_hour * HOURS_PER_YEAR,
        "co2e_modeled": False,
        "currency": "EUR",
    }
    return _record(
        rec_id,
        applied_at=_hour(-applied_hours_ago),
        cost_per_hour=billable_per_hour,
        co2e_per_hour=0.0,
        impact=impact,
        **updates,
    )


async def _accrue(db: LedgerDb, records, *, hours: int, now_offset: float = 0.05):
    attributor = SavingsAttributor(db.v1, CLUSTER, initial_backfill=timedelta(hours=hours), max_slots_per_run=hours + 4)
    return await attributor.accrue(records, now=_hour(0) + timedelta(hours=now_offset))


async def _entries(db: LedgerDb, **filters):
    return await db.repo.list_entries(CLUSTER_UID, filters=filters, limit=5000, ascending=True)


@pytest.mark.asyncio
@pytest.mark.database
async def test_projection_accrues_on_the_allocated_basis_when_nothing_is_billable(ledger: LedgerDb):
    # LED-01 acceptance 1: realization factor 0 (fixed capacity), applied 96 h ago, pending.
    record = _allocated_record(1, applied_hours_ago=96, realization=0.0)
    await _accrue(ledger, [record], hours=96)
    await _projector(ledger, records=[record]).sync(full=True)

    projected = await _entries(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST)
    assert len(projected) == 96
    assert math.fsum(entry.value for entry in projected) == pytest.approx(0.96, rel=1e-9)
    assert {entry.basis for entry in projected} == {BASIS_ALLOCATED}

    realization = realization_summary([record])
    assert realization.projected_allocated_cost_annual == pytest.approx(ALLOCATED_ANNUAL)
    assert realization.projected_billable_cost_annual_expected == pytest.approx(0.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_verified_change_is_restated_for_slots_without_a_projected_entry(ledger: LedgerDb):
    # LED-01 (c): a projection of exactly 0 wrote nothing; the measurement still restates the whole window.
    applied_at = _hour(-96)
    verified_at = _hour(-24)
    pending = _record(
        2, applied_at=applied_at, cost_per_hour=0.0, co2e_per_hour=0.0, impact={"allocated_cost_annual": 0.0}
    )
    attributor = SavingsAttributor(ledger.v1, CLUSTER, initial_backfill=timedelta(hours=100), max_slots_per_run=200)
    await attributor.accrue([pending], now=verified_at)
    verified = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": verified_at,
            "verification_status": "passed",
            "measured_cost_saved": ALLOCATED_ANNUAL,
        }
    )
    await attributor.accrue([verified], now=_hour(0) + timedelta(minutes=3))
    projector = _projector(ledger, records=[verified])
    await projector.sync(full=True)

    # Measured allocated over the whole life: 72 h restated before verification + 24 h accrued after.
    restated = await _net(ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST)
    accrued = await _net(ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST, book=BOOK_ACCRUED)
    assert accrued == pytest.approx(0.24, rel=1e-9)
    assert restated - accrued == pytest.approx(0.72, rel=1e-9)
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST) == pytest.approx(0.0, abs=1e-12)
    for entry in await _entries(ledger, method=METHOD_MEASURED_ALLOCATED):
        assert entry.basis == BASIS_ALLOCATED
    # Idempotent and chained.
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_exact_projection_equals_its_measurement_whatever_the_realization(ledger: LedgerDb):
    # LED-01 acceptance 3: factor 0.3 and an exact projection -> ratio 1.0, not 0.3.
    pending = _allocated_record(3, applied_hours_ago=72, realization=0.3)
    await _accrue(ledger, [pending], hours=72, now_offset=0.0)
    projector = _projector(ledger, records=[pending])
    await projector.sync(full=True)
    projected = await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST)
    assert projected == pytest.approx(0.72, rel=1e-9)

    verified = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(0),
            "verification_status": "passed",
            "measured_cost_saved": ALLOCATED_ANNUAL,
        }
    )
    restating = _projector(ledger, records=[verified])
    await restating.sync(full=True)
    measured: Optional[float] = await _net(ledger, method=METHOD_MEASURED_ALLOCATED, quantity=QUANTITY_COST)
    assert measured == pytest.approx(projected, rel=1e-9)
    assert await _net(ledger, method=METHOD_PROJECTED, quantity=QUANTITY_COST) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.asyncio
@pytest.mark.database
async def test_every_entry_carries_a_basis_and_old_entries_keep_their_hash(ledger: LedgerDb):
    from greenkube.api.routers.ledger import FOCUS_COLUMNS, _entry_payload, _focus_row

    # An entry written before the basis column existed (NULL) keeps a valid hash and reads with an inferred basis.
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("legacy:1")])
    legacy = _record(4, applied_at=_hour(-5))
    await _accrue(ledger, [legacy], hours=5)
    await _projector(ledger, records=[legacy]).sync(full=True)
    entries = await _entries(ledger)
    assert entries[0].basis is None
    assert all(_entry_payload(entry)["basis"] for entry in entries)
    assert _entry_payload(entries[0])["basis"] == BASIS_BILLABLE_EXPECTED
    # A record without an Impact Model projection keeps its legacy (billable) basis, labelled as such.
    assert {entry.basis for entry in entries[1:]} == {BASIS_BILLABLE_EXPECTED}
    row = dict(zip(FOCUS_COLUMNS, _focus_row(entries[1], None, 1.0)))
    assert row["x-greenkube-basis"] == BASIS_BILLABLE_EXPECTED
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok
