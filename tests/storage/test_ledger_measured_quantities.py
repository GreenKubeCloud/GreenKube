"""Savings ledger: released capacity and the cost of a reliability fix from the change measurement.

Released capacity accrues as its own quantities (core-hours, GiB-hours), never
converted to money; the measured cost of a reliability fix is booked as a
negative, labelled cost entry. Runs on SQLite and, with
``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import json
import math

import pytest

from greenkube.models.metrics import RecommendationStatus, RecommendationType
from greenkube.storage.change_repository import ChangeRepository
from greenkube.verify.ledger.model import (
    METHOD_MEASURED_ALLOCATED,
    QUANTITY_COST,
    QUANTITY_CPU_CORE_HOURS,
    QUANTITY_MEM_GIB_HOURS,
    REASON_COST_OF_FIX,
)
from greenkube.verify.ledger.verification import verify_chain
from tests.storage import test_ledger_v2_contract as _contract  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_basis import HOURS_PER_YEAR, _allocated_record  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]
    CLUSTER_UID,
    LedgerDb,
    _hour,
    _projector,
)
from tests.verify.fixtures import change_event  # pyrefly: ignore[missing-import]

ledger = _contract.ledger  # the SQLite and PostgreSQL fixture


async def _measured_change(db: LedgerDb, change_id: str, rec_id: int, *, capacity: dict, params: dict) -> None:
    await ChangeRepository(db.manager).insert_change(
        change_event(_hour(-10), change_id=change_id, recommendation_ids=[rec_id], name=f"api-{rec_id}")
    )
    allocations = [{"recommendation_id": rec_id, "quantity": "cost_cpu", "share": 1.0, "basis": "request_reduction"}]
    decided = _hour(-2).isoformat()
    await db.execute(
        "INSERT INTO change_measurements (measurement_id, change_id, sequence, strategy_key, strategy_version, "
        "windows, counterfactual_params, allocations, outcome, evidence_sha256, decided_at, capacity) VALUES ("
        f"'m-{change_id}', '{change_id}', 1, 'request_rightsizing', '2.0', '{{}}', '{json.dumps(params)}', "
        f"'{json.dumps(allocations)}', 'realized', 'sha', '{decided}', '{json.dumps(capacity)}')"
    )


def _verified(rec_id: int, measured_cost: float):
    return _allocated_record(rec_id, applied_hours_ago=10, realization=1.0).model_copy(
        update={
            "type": RecommendationType.RIGHTSIZING_CPU,
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(-2),
            "verification_status": "passed",
            "measured_cost_saved": measured_cost,
        }
    )


async def _entries(db: LedgerDb, rec_id: int, quantity: str):
    return await db.repo.list_entries(
        CLUSTER_UID, filters={"recommendation_id": rec_id, "quantity": quantity}, limit=5000
    )


@pytest.mark.asyncio
@pytest.mark.database
async def test_released_capacity_accrues_as_its_own_quantities(ledger: LedgerDb):
    record = _verified(7, 0.01 * HOURS_PER_YEAR)
    capacity = {"verdict": "released", "total": {"cpu_cores": 0.5, "mem_gib": 1.0}}
    await _measured_change(ledger, "chg-cap", 7, capacity=capacity, params={})
    projector = _projector(ledger, records=[record])
    await projector.sync(full=True)

    cpu = await _entries(ledger, 7, QUANTITY_CPU_CORE_HOURS)
    assert math.fsum(entry.value for entry in cpu) == pytest.approx(5.0, rel=1e-9)  # 0.5 core x 10 h
    assert {(entry.unit, entry.currency, entry.method) for entry in cpu} == {
        ("core-hours", None, METHOD_MEASURED_ALLOCATED)
    }
    # A CPU request change releases no memory of its own.
    assert await _entries(ledger, 7, QUANTITY_MEM_GIB_HOURS) == []
    # Never money: the cost total is the measured saving alone (8 h restated before verification at 0.01/h).
    breakdown = await ledger.repo.accrual_breakdown(CLUSTER_UID)
    assert math.fsum(row.cost_saved_dollars for row in breakdown) == pytest.approx(0.08, rel=1e-9)
    assert (await projector.sync(full=True)).written == 0
    assert (await verify_chain(ledger.repo, CLUSTER_UID)).ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_the_cost_of_a_reliability_fix_is_booked_as_a_labelled_negative_entry(ledger: LedgerDb):
    record = _verified(8, 0.0)
    params = {"objective": "reliability", "cost_of_fix_annual": 0.02 * HOURS_PER_YEAR}
    capacity = {"verdict": "released", "total": {"cpu_cores": -0.25, "mem_gib": None}}
    await _measured_change(ledger, "chg-fix", 8, capacity=capacity, params=params)
    await _projector(ledger, records=[record]).sync(full=True)

    costs = await _entries(ledger, 8, QUANTITY_COST)
    assert costs and {entry.reason for entry in costs} == {REASON_COST_OF_FIX}
    assert math.fsum(entry.value for entry in costs) == pytest.approx(-0.2, rel=1e-9)  # 0.02/h x 10 h
    cpu = await _entries(ledger, 8, QUANTITY_CPU_CORE_HOURS)
    assert math.fsum(entry.value for entry in cpu) == pytest.approx(-2.5, rel=1e-9)  # an increase reserves
    # Review WP-09 point 2: the cost of a fix is never netted into the measured savings or their counters.
    breakdown = await ledger.repo.accrual_breakdown(CLUSTER_UID)
    assert math.fsum(row.cost_saved_dollars for row in breakdown if row.method == "measured") == pytest.approx(
        0.0, abs=1e-12
    )
    counters = await ledger.repo.lifetime_counters(CLUSTER_UID)
    assert math.fsum(row.reversed_cost_dollars + row.accrued_cost_dollars for row in counters) == pytest.approx(
        0.0, abs=1e-12
    )
    fixes = await ledger.repo.accrual_breakdown(CLUSTER_UID, reasons=(REASON_COST_OF_FIX,))
    assert math.fsum(row.cost_saved_dollars for row in fixes) == pytest.approx(-0.2, rel=1e-9)
    rows = await ledger.repo.summary(CLUSTER_UID)
    assert {row.reason_group for row in rows if row.quantity == QUANTITY_COST} == {REASON_COST_OF_FIX}

    # Its own block in the savings report.
    from greenkube.core.savings_report import assemble_savings_report

    report = assemble_savings_report([record], breakdown, cost_of_fix=fixes)
    assert report.realized_to_date.cost == pytest.approx(0.0, abs=1e-12)
    assert report.reliability_cost_to_date is not None
    assert report.reliability_cost_to_date.cost == pytest.approx(-0.2, rel=1e-9)
