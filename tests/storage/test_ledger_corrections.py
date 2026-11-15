"""Savings ledger WP-09.3: refuted projections, measured losses and state-derived supersession (LED-03, LED-08).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.core.savings_report import assemble_savings_report
from greenkube.models.metrics import RecommendationStatus
from greenkube.models.savings import SavingsLedgerRecord
from greenkube.verify.ledger.model import (
    METHOD_MEASURED_ALLOCATED,
    METHOD_PROJECTED,
    QUANTITY_COST,
    REASON_NOT_REALIZED,
)
from greenkube.verify.ledger.verification import verify_chain
from tests.storage import test_ledger_v2_contract as _contract  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_basis import HOURS_PER_YEAR, _allocated_record  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]
    CLUSTER,
    CLUSTER_UID,
    LedgerDb,
    _hour,
    _projector,
)

ledger = _contract.ledger  # the SQLite and PostgreSQL fixture


async def _net_of(db: LedgerDb, rec_id: int, method: str) -> float:
    entries = await db.repo.list_entries(
        CLUSTER_UID, filters={"recommendation_id": rec_id, "method": method, "quantity": QUANTITY_COST}, limit=5000
    )
    return sum(entry.value for entry in entries)


def _attributor(db: LedgerDb, hours: int = 100) -> SavingsAttributor:
    return SavingsAttributor(db.v1, CLUSTER, initial_backfill=timedelta(hours=hours), max_slots_per_run=hours + 4)


@pytest.mark.asyncio
@pytest.mark.database
async def test_refuted_projections_are_reversed_and_measured_losses_booked(ledger: LedgerDb):
    # LED-03 (SIM1): three changes accrue 72 h of projection (0.01/h allocated) before their verdict.
    pending = [_allocated_record(rec_id, applied_hours_ago=72, realization=1.0) for rec_id in (1, 2, 3)]
    await _attributor(ledger).accrue(pending, now=_hour(0) + timedelta(minutes=3))
    await _projector(ledger, records=pending).sync(full=True)
    decided = {
        "verification_status": "failed",
        "verification_window_end": _hour(0),
    }
    # 1: not realized, the change costs 0.005/h more (a measured loss).
    loss = pending[0].model_copy(update={**decided, "measured_cost_saved": -0.005 * HOURS_PER_YEAR})
    # 2: inconclusive, the projection stays but is unverified.
    undecided = pending[1].model_copy(update={"verification_status": "inconclusive"})
    # 3: not realized without a signed measured value.
    refuted = pending[2].model_copy(update=decided)
    records = [loss, undecided, refuted]
    projector = _projector(ledger, records=records)
    await projector.sync(full=True)

    assert await _net_of(ledger, 1, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert await _net_of(ledger, 1, METHOD_MEASURED_ALLOCATED) == pytest.approx(-0.36, rel=1e-9)
    assert await _net_of(ledger, 2, METHOD_PROJECTED) == pytest.approx(0.72, rel=1e-9)
    assert await _net_of(ledger, 3, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert await _net_of(ledger, 3, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.0, abs=1e-12)
    reversals = await ledger.repo.list_entries(CLUSTER_UID, filters={"reason": REASON_NOT_REALIZED}, limit=5000)
    assert {entry.recommendation_id for entry in reversals} == {3}

    # The report keeps the refuted projections out and labels the inconclusive one unverified.
    uid = CLUSTER_UID
    report = assemble_savings_report(
        records,
        await ledger.repo.accrual_breakdown(uid),
        projected_unverified=await ledger.repo.accrual_breakdown(uid, recommendation_ids=[2]),
    )
    assert report.projected_to_date.cost == pytest.approx(0.0, abs=1e-12)
    assert report.projected_unverified_to_date is not None
    assert report.projected_unverified_to_date.cost == pytest.approx(0.72, rel=1e-9)
    assert report.realized_to_date.cost == pytest.approx(-0.36, rel=1e-9)

    # State-derived and idempotent.
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_supersession_is_derived_from_the_recommendation_state(ledger: LedgerDb):
    # LED-08 acceptance 1 and 2: the one-shot supersede call failed, and a late accrual row lands after it.
    pending = _allocated_record(5, applied_hours_ago=10, realization=1.0)
    await _attributor(ledger).accrue([pending], now=_hour(0) + timedelta(minutes=3))
    review = pending.model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})
    projector = _projector(ledger, records=[review])
    await projector.sync(full=True)
    assert await _net_of(ledger, 5, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    from greenkube.verify.ledger.projector import LEDGER_UNRECONCILED_RECOMMENDATIONS

    assert LEDGER_UNRECONCILED_RECOMMENDATIONS._value.get() == 1.0
    await projector.sync(full=True)
    assert LEDGER_UNRECONCILED_RECOMMENDATIONS._value.get() == 0.0

    late = SavingsLedgerRecord(
        recommendation_id=5,
        cluster_name=CLUSTER,
        namespace="prod",
        recommendation_type="RIGHTSIZING_CPU",
        cost_saved_dollars=0.01,
        period_seconds=3600,
        timestamp=_hour(1),
        period_start=_hour(0),
        period_end=_hour(1),
        measurement_method="projected",
    )
    await ledger.v1.save_records([late])
    await projector.sync(full=True)
    assert await _net_of(ledger, 5, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_dismissed_rollback_review_reinstates_the_accruals_before_it(ledger: LedgerDb):
    # LED-08 acceptance 3: rollback_review, then dismissed -> accruals before the review plus after the dismissal.
    pending = _allocated_record(6, applied_hours_ago=10, realization=1.0)
    attributor = _attributor(ledger, hours=20)
    await attributor.accrue([pending], now=_hour(-5) + timedelta(minutes=3))
    review = pending.model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})
    await _projector(ledger, records=[review]).sync(full=True)
    assert await _net_of(ledger, 6, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)

    dismissed = pending.model_copy(update={"status": RecommendationStatus.APPLIED, "verification_status": "pending"})
    await attributor.accrue([dismissed], now=_hour(0) + timedelta(minutes=3))
    projector = _projector(ledger, records=[dismissed])
    await projector.sync(full=True)
    assert await _net_of(ledger, 6, METHOD_PROJECTED) == pytest.approx(0.10, rel=1e-9)
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_reversal_with_the_wrong_sign_fails_verification_and_the_head_is_published(ledger: LedgerDb):
    # LED-05 acceptance 4 and fix 2a.
    from dataclasses import replace

    from greenkube.verify.ledger.projector import reversal_of
    from greenkube.verify.ledger.verification import CHAIN_HEAD_SEQ, verify_ledger
    from tests.storage.test_ledger_v2_contract import _draft  # pyrefly: ignore[missing-import]

    [entry] = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("target", 2.0)])
    good = reversal_of(entry, "supersession", created_by="test")
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [good])
    result = await verify_ledger(ledger.repo, checkpoints={})
    assert result[CLUSTER_UID].ok
    assert CHAIN_HEAD_SEQ.labels(cluster_uid=CLUSTER_UID)._value.get() == 2.0

    [other] = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("target-2", 3.0)])
    wrong = replace(reversal_of(other, "supersession", created_by="test"), value=3.0)
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [wrong])
    failed = await verify_chain(ledger.repo, CLUSTER_UID)
    assert not failed.ok and failed.reason == "reversal_mismatch"


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_re_measurement_restates_from_the_new_measurement(ledger: LedgerDb, monkeypatch):
    # LED-03: verified at 0.01/h by m1, re-measured verified at 0.004/h by m2 -> restated at m2, once.
    from greenkube.storage.ledger_repository import Provenance

    pending = _allocated_record(9, applied_hours_ago=10, realization=1.0)
    await _attributor(ledger, hours=20).accrue([pending], now=_hour(0) + timedelta(minutes=3))
    current = {"measurement": "m1"}
    original = ledger.repo.provenance

    async def provenance(ids):
        found = await original(ids)
        return {rec_id: Provenance(measurement_id=current["measurement"]) for rec_id in found}

    monkeypatch.setattr(ledger.repo, "provenance", provenance)
    first = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(0),
            "verification_status": "passed",
            "measured_cost_saved": 0.01 * HOURS_PER_YEAR,
        }
    )
    await _projector(ledger, records=[first]).sync(full=True)
    assert await _net_of(ledger, 9, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.10, rel=1e-9)

    current["measurement"] = "m2"
    second = first.model_copy(update={"measured_cost_saved": 0.004 * HOURS_PER_YEAR})
    projector = _projector(ledger, records=[second])
    await projector.sync(full=True)
    assert await _net_of(ledger, 9, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.04, rel=1e-9)
    assert await _net_of(ledger, 9, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_refuted_projection_verified_by_a_re_measurement_gets_its_measured_amount(ledger: LedgerDb):
    pending = _allocated_record(10, applied_hours_ago=10, realization=1.0)
    await _attributor(ledger, hours=20).accrue([pending], now=_hour(0) + timedelta(minutes=3))
    refuted = pending.model_copy(update={"verification_status": "failed", "verification_window_end": _hour(0)})
    await _projector(ledger, records=[refuted]).sync(full=True)
    assert await _net_of(ledger, 10, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)

    verified = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(0),
            "verification_status": "passed",
            "measured_cost_saved": 0.008 * HOURS_PER_YEAR,
        }
    )
    projector = _projector(ledger, records=[verified])
    await projector.sync(full=True)
    assert await _net_of(ledger, 10, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.08, rel=1e-9)
    assert await _net_of(ledger, 10, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert (await projector.sync(full=True)).written == 0


async def _remeasured(ledger: LedgerDb, monkeypatch, rec_id: int, *, applied_hours_ago: int):
    """Verified by m1 at 0.01/h, re-measured by m2 at 0.004/h (review WP-09 point 1)."""
    from greenkube.storage.ledger_repository import Provenance

    pending = _allocated_record(rec_id, applied_hours_ago=applied_hours_ago, realization=1.0)
    await _attributor(ledger, hours=20).accrue([pending], now=_hour(0) + timedelta(minutes=3))
    current = {"measurement": "m1"}
    original = ledger.repo.provenance

    async def provenance(ids):
        found = await original(ids)
        return {item: Provenance(measurement_id=current["measurement"]) for item in found}

    monkeypatch.setattr(ledger.repo, "provenance", provenance)
    first = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(0),
            "verification_status": "passed",
            "measured_cost_saved": 0.01 * HOURS_PER_YEAR,
        }
    )
    await _projector(ledger, records=[first]).sync(full=True)
    current["measurement"] = "m2"
    second = first.model_copy(update={"measured_cost_saved": 0.004 * HOURS_PER_YEAR})
    await _projector(ledger, records=[second]).sync(full=True)
    return second


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rollback_after_a_re_measurement_reverses_each_restatement_once(ledger: LedgerDb, monkeypatch):
    remeasured = await _remeasured(ledger, monkeypatch, 11, applied_hours_ago=10)
    review = remeasured.model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})
    projector = _projector(ledger, records=[review])
    await projector.sync(full=True)
    await projector.sync(full=True)
    assert await _net_of(ledger, 11, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.0, abs=1e-12)
    assert await _net_of(ledger, 11, METHOD_PROJECTED) == pytest.approx(0.0, abs=1e-12)
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rollback_after_a_re_measurement_reverses_slot_restatements_once(ledger: LedgerDb, monkeypatch):
    # Applied 30 h ago but the attribution backfills 20 h only: 10 slots are covered by slot restatements.
    remeasured = await _remeasured(ledger, monkeypatch, 12, applied_hours_ago=30)
    assert await _net_of(ledger, 12, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.004 * 30, rel=1e-9)
    review = remeasured.model_copy(update={"status": RecommendationStatus.ROLLBACK_REVIEW})
    projector = _projector(ledger, records=[review])
    await projector.sync(full=True)
    await projector.sync(full=True)
    assert await _net_of(ledger, 12, METHOD_MEASURED_ALLOCATED) == pytest.approx(0.0, abs=1e-12)
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_entry_reversed_twice_fails_verification(ledger: LedgerDb):
    from greenkube.verify.ledger.projector import reversal_of
    from tests.storage.test_ledger_v2_contract import _draft  # pyrefly: ignore[missing-import]

    [entry] = await ledger.repo.append(CLUSTER_UID, CLUSTER, [_draft("twice", 2.0)])
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [reversal_of(entry, "restatement", created_by="test")])
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok
    await ledger.repo.append(CLUSTER_UID, CLUSTER, [reversal_of(entry, "supersession", created_by="test")])
    failed = await verify_chain(ledger.repo, CLUSTER_UID)
    assert not failed.ok and failed.reason == "double_reversal"
    # An incremental walk that starts after the first reversal sees it too.
    [second] = await ledger.repo.chain_page(CLUSTER_UID, 1, 1)
    later = await verify_chain(ledger.repo, CLUSTER_UID, after_seq=2, after_hash=second.hash)
    assert not later.ok and later.reason == "double_reversal"
