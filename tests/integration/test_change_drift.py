"""Drift monitor on real databases (C8): regression, reversal entries, events and spec drift.

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.models.savings import METHOD_MEASURED, SavingsLedgerRecord
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.verify.drift import REVERSAL_SETTLE, DriftMonitor, DriftSettings
from greenkube.verify.model import MeasurementState, TimeWindow, WorkloadSpecObservation
from greenkube.verify.service import DECISION_GRACE
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import Scenario, hour_now, record_status  # pyrefly: ignore[missing-import]

CLUSTER = "drift-test"
HOURS_PER_YEAR = 365.25 * 24


def _savings_repo(manager):
    if manager.db_type == "postgres":
        return PostgresSavingsLedgerRepository(manager)
    return SQLiteSavingsLedgerRepository(manager)


async def _realized(manager, *, drift_days: int):
    """A change realized 9 days ago whose CPU request creeps back for the last ``drift_days`` days."""
    scenario = Scenario(manager)
    today = hour_now().replace(hour=0)
    scenario.completed = today - timedelta(days=16)
    scenario.decision = scenario.completed - timedelta(hours=2)
    completed = scenario.completed
    drift_start = today - timedelta(days=drift_days)
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(TimeWindow(start=completed, end=drift_start), cpu_request=300, memory_request=1024**3)
    scenario.add_rows(
        TimeWindow(start=drift_start, end=hour_now() + timedelta(hours=1)), cpu_request=1000, memory_request=1024**3
    )
    change, applied = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    decided_at = status.post_window_end + DECISION_GRACE
    await scenario.service.run_due(now=decided_at)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    for record in applied:
        assert record.id is not None
        # Verified at the decision (the lifecycle stamps the wall clock).
        await scenario.reco_repo.update_recommendation_fields(record.id, {"verified_at": decided_at})
    return scenario, change, applied, decided_at


async def _accrue(repo, record, start: datetime, end: datetime) -> None:
    """Measured hourly accruals of ``record`` over ``[start, end)``, as the attributor writes them."""
    rows = []
    slot = start
    while slot < end:
        rows.append(
            SavingsLedgerRecord(
                recommendation_id=record.id,
                cluster_name=CLUSTER,
                namespace=record.namespace or "",
                recommendation_type=record.type.value,
                co2e_saved_grams=0.0,
                cost_saved_dollars=record.measured_cost_saved / HOURS_PER_YEAR,
                period_seconds=3600,
                timestamp=slot + timedelta(hours=1),
                period_start=slot,
                period_end=slot + timedelta(hours=1),
                slot_start=slot,
                measurement_method=METHOD_MEASURED,
            )
        )
        slot += timedelta(hours=1)
    await repo.save_records(rows)


def _monitor(scenario: Scenario, repo) -> DriftMonitor:
    return DriftMonitor(
        scenario.service,
        savings_repo=repo,
        settings=DriftSettings(threshold=0.5, sustained_days=3),
        cluster_name=CLUSTER,
    )


async def test_a_sustained_drift_regresses_the_change_and_reverses_its_accruals(database):  # noqa: F811
    scenario, change, applied, decided_at = await _realized(database, drift_days=4)
    repo = _savings_repo(database)
    records = await scenario.records()
    now = hour_now() + timedelta(minutes=30)
    for record in records:
        await _accrue(repo, record, decided_at.replace(minute=0) + timedelta(hours=1), hour_now())
    monitor = _monitor(scenario, repo)

    result = await monitor.run(now=now)

    assert result["regressed"] == [change.change_id] and result["reversed"] == []
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REGRESSED
    events = await scenario.changes.list_events(change.change_id)
    regressed = events[-1]
    assert regressed.event_type == "regressed" and regressed.actor == "drift-monitor"
    onset = hour_now().replace(hour=0) - timedelta(days=4)
    assert regressed.payload["onset"] == onset.isoformat()
    assert all(day["lost_share"] == pytest.approx(0.85, abs=0.01) for day in regressed.payload["days"][-4:])
    assert all(record_status(record) == "regressed" for record in await scenario.records())

    # Nothing is reversed before the accrual has settled, then exactly once.
    assert (await monitor.run(now=now + REVERSAL_SETTLE / 2))["reversed"] == []
    later = now + REVERSAL_SETTLE + timedelta(minutes=1)
    assert (await monitor.run(now=later))["reversed"] == [change.change_id]
    assert (await monitor.run(now=later + timedelta(hours=1)))["reversed"] == []

    counters = {row.recommendation_type: row for row in await repo.get_lifetime_counters(CLUSTER)}
    for record in records:
        counter = counters[record.type.value]
        expected = record.measured_cost_saved / HOURS_PER_YEAR * (hour_now() - onset).total_seconds() / 3600
        # The measured accruals since the onset are reversed, the earlier ones are kept.
        assert counter.reversed_cost_dollars == pytest.approx(expected, rel=1e-6)
        assert counter.accrued_cost_dollars > counter.reversed_cost_dollars
    reversed_events = [
        event
        for event in await scenario.changes.list_events(change.change_id)
        if event.event_type == "savings_reversed"
    ]
    assert len(reversed_events) == 1
    assert sorted(entry["recommendation_id"] for entry in reversed_events[0].payload["entries"]) == sorted(
        record.id for record in records
    )


async def test_a_holding_saving_stays_realized(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=2)
    result = await _monitor(scenario, _savings_repo(database)).run(now=hour_now() + timedelta(minutes=30))
    assert result == {"regressed": [], "closed": [], "reversed": []}
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    assert all(record_status(record) == "verified" for record in await scenario.records())


async def test_a_spec_drift_after_the_measurement_closes_the_change(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=0)
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid="uid-api",
                generation=5,
                spec_digest="digest-image-bump",
                observed_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        ]
    )
    result = await _monitor(scenario, _savings_repo(database)).run(now=hour_now() + timedelta(minutes=30))
    assert result["closed"] == [change.change_id]
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CLOSED
    assert (await scenario.changes.list_events(change.change_id))[-1].event_type == "spec_drift"
