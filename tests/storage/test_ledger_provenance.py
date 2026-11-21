"""Ledger entries reproducible from their own provenance (LED-11) on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.models.metrics import RecommendationStatus
from greenkube.verify.ledger.model import ENTRY_ACCRUAL, METHOD_MEASURED_ALLOCATED
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER,
    CLUSTER_UID,
    YEAR_SECONDS,
    LedgerDb,
    _accrue_realistic,
    _hour,
    _projector,
    _record,
    ledger,
)


@pytest.mark.asyncio
@pytest.mark.database
async def test_every_accrual_entry_carries_the_annual_rate_it_was_computed_from(ledger: LedgerDb):  # noqa: F811
    await _accrue_realistic(ledger)
    await _projector(ledger).sync()
    entries = await ledger.repo.list_entries(CLUSTER_UID, limit=10_000, ascending=True)
    accruals = [entry for entry in entries if entry.entry_kind == ENTRY_ACCRUAL]
    assert accruals
    for entry in accruals:
        assert entry.annual_rate is not None
        seconds = (entry.period_end - entry.period_start).total_seconds()
        assert entry.value == pytest.approx(entry.annual_rate * seconds / YEAR_SECONDS, abs=1e-12)


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_measured_entry_names_the_measurement_of_its_rate_at_accrual_time(ledger: LedgerDb):  # noqa: F811
    from greenkube.core.savings_attributor import SavingsAttributor

    hours_per_year = YEAR_SECONDS / 3600
    record = _record(
        7,
        applied_at=_hour(-6),
        status=RecommendationStatus.VERIFIED,
        verified_at=_hour(-5),
        verification_status="passed",
        measured_cost_saved=0.2 * hours_per_year,
        measured_co2e_saved_grams=0.0,
        baseline={"latest_measurement_id": "m-first"},
    )
    attributor = SavingsAttributor(ledger.v1, CLUSTER, initial_backfill=timedelta(hours=8))
    await attributor.accrue([record], now=_hour(0) + timedelta(minutes=1))
    await _projector(ledger).sync()
    measured = await ledger.repo.list_entries(CLUSTER_UID, filters={"method": METHOD_MEASURED_ALLOCATED}, limit=100)
    assert measured
    assert {entry.measurement_id for entry in measured} == {"m-first"}
    assert [entry.annual_rate for entry in measured] == [pytest.approx(0.2 * hours_per_year)] * len(measured)


@pytest.mark.asyncio
@pytest.mark.database
async def test_restatements_carry_the_measured_rate_they_were_computed_from(ledger: LedgerDb):  # noqa: F811
    from greenkube.core.savings_attributor import SavingsAttributor
    from greenkube.verify.ledger.model import ENTRY_RESTATEMENT

    hours_per_year = YEAR_SECONDS / 3600
    applied = _record(8, applied_at=_hour(-6))
    attributor = SavingsAttributor(ledger.v1, CLUSTER, initial_backfill=timedelta(hours=8))
    await attributor.accrue([applied], now=_hour(-2) + timedelta(minutes=1))
    verified = _record(
        8,
        applied_at=_hour(-6),
        status=RecommendationStatus.VERIFIED,
        verified_at=_hour(-2),
        verification_status="passed",
        measured_cost_saved=0.3 * hours_per_year,
        measured_co2e_saved_grams=0.5 * hours_per_year,
    )
    await _projector(ledger, [verified]).sync()
    entries = await ledger.repo.list_entries(CLUSTER_UID, limit=1000, ascending=True)
    restatements = [entry for entry in entries if entry.entry_kind == ENTRY_RESTATEMENT]
    assert restatements
    for entry in restatements:
        if entry.annual_rate is None:
            continue  # slot restatements of uncovered parts carry no single rate
        seconds = (entry.period_end - entry.period_start).total_seconds()
        assert entry.value == pytest.approx(entry.annual_rate * seconds / YEAR_SECONDS, abs=1e-12)
    assert any(entry.annual_rate is not None for entry in restatements)
