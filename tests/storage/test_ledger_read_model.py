"""Savings ledger WP-09.2: user-facing savings numbers come from ledger v2 (LED-04).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationStatus
from greenkube.models.savings import METHOD_MEASURED, METHOD_PROJECTED
from tests.storage import test_ledger_v2_contract as _contract  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_basis import ALLOCATED_ANNUAL, _allocated_record  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]
    CLUSTER,
    CLUSTER_UID,
    LedgerDb,
    _hour,
    _projector,
)

ledger = _contract.ledger  # the SQLite and PostgreSQL fixture


class _Recommendations:
    def __init__(self, records):
        self.records = records

    async def get_applied_recommendations(self, namespace=None):
        return [record for record in self.records if namespace is None or record.namespace == namespace]


async def _verified_change(db: LedgerDb):
    """Applied 96 h ago (allocated 0.01/h, realization 0), verified 24 h ago at 0.01/h, projected into v2."""
    pending = _allocated_record(1, applied_hours_ago=96, realization=0.0)
    attributor = SavingsAttributor(db.v1, CLUSTER, initial_backfill=timedelta(hours=100), max_slots_per_run=200)
    await attributor.accrue([pending], now=_hour(-24))
    verified = pending.model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": _hour(-24),
            "verification_status": "passed",
            "measured_cost_saved": ALLOCATED_ANNUAL,
        }
    )
    await attributor.accrue([verified], now=_hour(0) + timedelta(minutes=3))
    await _projector(db, records=[verified]).sync(full=True)
    return verified


@pytest.mark.asyncio
@pytest.mark.database
async def test_savings_report_serves_the_restated_ledger(ledger: LedgerDb, monkeypatch):
    from greenkube.api.routers import recommendations as router

    verified = await _verified_change(ledger)
    monkeypatch.setattr(router, "_get_savings_cluster_name", lambda: CLUSTER)
    report = await router.get_savings_summary(
        namespace=None,
        last=None,
        reco_repo=_Recommendations([verified]),  # type: ignore[arg-type]
        savings_repo=ledger.v1,
        ledger_repo=ledger.repo,
    )
    # 72 h restated from the projection + 24 h measured after verification.
    assert report.realized_to_date.cost == pytest.approx(0.96, rel=1e-9)
    assert report.projected_to_date.cost == pytest.approx(0.0, abs=1e-12)
    assert report.realized_to_date.source == "ledger_v2"
    # The accrued book alone (what v1 says) stays visible for the transition.
    assert report.realized_to_date.accrued_only_cost == pytest.approx(0.24, rel=1e-9)
    assert report.projected_to_date.accrued_only_cost == pytest.approx(0.72, rel=1e-9)
    summary = {
        (row.method, row.quantity): row for row in await ledger.repo.summary(CLUSTER_UID) if row.quantity == "cost"
    }
    restated = sum(row.value for row in await ledger.repo.summary(CLUSTER_UID) if row.method == "measured_allocated")
    assert summary and report.realized_to_date.cost == pytest.approx(restated, rel=1e-12)


@pytest.mark.asyncio
@pytest.mark.database
async def test_counters_and_dashboard_windows_come_from_ledger_v2(ledger: LedgerDb):
    await _verified_change(ledger)
    counters = {(row.method, row.recommendation_type): row for row in await ledger.repo.lifetime_counters(CLUSTER_UID)}
    projected = counters[(METHOD_PROJECTED, "RIGHTSIZING_CPU")]
    measured = counters[(METHOD_MEASURED, "RIGHTSIZING_CPU")]
    # Restatement reversals are visible to Prometheus: accrued 0.72 projected, reversed 0.72.
    assert projected.accrued_cost_dollars == pytest.approx(0.72, rel=1e-9)
    assert projected.reversed_cost_dollars == pytest.approx(0.72, rel=1e-9)
    assert measured.accrued_cost_dollars == pytest.approx(0.96, rel=1e-9)
    assert measured.reversed_cost_dollars == pytest.approx(0.0, abs=1e-12)

    breakdown = await ledger.repo.accrual_breakdown(CLUSTER_UID, _hour(-200), _hour(1))
    by_method = {}
    for row in breakdown:
        by_method[row.method] = by_method.get(row.method, 0.0) + row.cost_saved_dollars
    assert by_method[METHOD_MEASURED] == pytest.approx(0.96, rel=1e-9)
    assert by_method.get(METHOD_PROJECTED, 0.0) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.asyncio
@pytest.mark.database
async def test_v1_ledger_is_append_only_except_supersession_and_reconciles(ledger: LedgerDb):
    from greenkube.verify.ledger.reconciliation import LEDGER_RECONCILIATION_OK, reconcile

    await _verified_change(ledger)
    with pytest.raises(Exception, match="append-only"):
        await ledger.execute("UPDATE recommendation_savings_ledger SET cost_saved_dollars = 99")
    with pytest.raises(Exception, match="append-only"):
        await ledger.execute("DELETE FROM recommendation_savings_ledger")
    truthy = "TRUE" if ledger.backend == "postgres" else "1"
    falsy = "FALSE" if ledger.backend == "postgres" else "0"
    await ledger.execute(f"UPDATE recommendation_savings_ledger SET superseded = {truthy}")
    with pytest.raises(Exception, match="append-only"):
        await ledger.execute(f"UPDATE recommendation_savings_ledger SET superseded = {falsy}")

    # Superseded rows are reversed in v2 by the next pass: the accrued book still reconciles.
    assert not await reconcile(ledger.repo, CLUSTER_UID, CLUSTER)
    await _projector(ledger).sync(full=True)
    assert await reconcile(ledger.repo, CLUSTER_UID, CLUSTER)
    assert LEDGER_RECONCILIATION_OK.labels(cluster_uid=CLUSTER_UID)._value.get() == 1.0
