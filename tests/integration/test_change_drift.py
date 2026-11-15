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


async def _realized(manager, *, drift_days: int, digests: bool = False):
    """A change realized 9 days ago whose CPU request creeps back for the last ``drift_days`` days.

    ``digests``: the change records resources and image digests (``res-1``, ``img-1``, MEA-08).
    """
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
    change, applied = await scenario.apply(
        resources_digest="res-1" if digests else None, image_digest="img-1" if digests else None
    )
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
        # Its own memo: the tests share change ids.
        assessed={},
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
    monitor = _monitor(scenario, _savings_repo(database))
    result = await monitor.run(now=hour_now() + timedelta(minutes=30))
    assert result == {"regressed": [], "closed": [], "reversed": []}
    # Drift is decided on complete days: a second pass the same day reads no metrics.
    reads = len(scenario.metrics.reads)
    assert reads > 0
    await monitor.run(now=hour_now() + timedelta(minutes=45))
    assert len(scenario.metrics.reads) == reads
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


async def _observe(scenario: Scenario, digest: str, observed_at: datetime, uid: str = "uid-api") -> None:
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid=uid,
                generation=7,
                spec_digest=digest,
                observed_at=observed_at,
            )
        ]
    )


async def _reversed(repo, records) -> dict:
    counters = {row.recommendation_type: row for row in await repo.get_lifetime_counters(CLUSTER)}
    return {record.id: counters[record.type.value].reversed_cost_dollars for record in records}


def _hours_of(record, start: datetime, end: datetime) -> float:
    return record.measured_cost_saved / HOURS_PER_YEAR * (end - start).total_seconds() / 3600


async def test_a_reverted_template_with_drifted_usage_regresses_and_reverses(database):  # noqa: F811
    """Requests reverted in git: a new template digest and usage back near the baseline."""
    scenario, change, _, decided_at = await _realized(database, drift_days=4)
    onset = hour_now().replace(hour=0) - timedelta(days=4)
    await _observe(scenario, "digest-reverted", onset + timedelta(minutes=13))
    repo = _savings_repo(database)
    records = await scenario.records()
    for record in records:
        await _accrue(repo, record, decided_at.replace(minute=0) + timedelta(hours=1), hour_now())
    monitor = _monitor(scenario, repo)
    now = hour_now() + timedelta(minutes=30)

    result = await monitor.run(now=now)

    assert result["regressed"] == [change.change_id] and result["closed"] == []
    assert not [record for record in await scenario.records() if record_status(record) == "verified"]
    later = now + REVERSAL_SETTLE + timedelta(minutes=1)
    assert (await monitor.run(now=later))["reversed"] == [change.change_id]
    reversed_cost = await _reversed(repo, records)
    for record in records:
        assert reversed_cost[record.id] == pytest.approx(_hours_of(record, onset, hour_now()), rel=1e-6)


async def test_a_spec_drift_ends_the_measured_accrual_and_reverses_what_followed(database):  # noqa: F811
    """A template change while the saving holds: accrual stops at the change and what followed is reversed."""
    from greenkube.core.savings_attributor import accrual_components

    scenario, change, _, decided_at = await _realized(database, drift_days=0)
    observed_at = hour_now() - timedelta(days=1) + timedelta(minutes=17)
    await _observe(scenario, "digest-image-bump", observed_at)
    repo = _savings_repo(database)
    records = await scenario.records()
    for record in records:
        await _accrue(repo, record, decided_at.replace(minute=0) + timedelta(hours=1), hour_now())
    monitor = _monitor(scenario, repo)
    now = hour_now() + timedelta(minutes=30)

    result = await monitor.run(now=now)

    assert result["closed"] == [change.change_id] and result["regressed"] == []
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CLOSED
    assert status.details["accrual_end"] == observed_at.isoformat()
    event = (await scenario.changes.list_events(change.change_id))[-1]
    assert event.event_type == "spec_drift" and event.payload["accrual_end"] == observed_at.isoformat()
    members = await scenario.records()
    assert members and all(record_status(record) == "regressed" for record in members)
    # The attributor no longer accrues anything for them.
    assert all(accrual_components(record) == [] for record in members)

    assert (await monitor.run(now=now + REVERSAL_SETTLE / 2))["reversed"] == []
    later = now + REVERSAL_SETTLE + timedelta(minutes=1)
    assert (await monitor.run(now=later))["reversed"] == [change.change_id]
    reversed_cost = await _reversed(repo, records)
    for record in records:
        assert reversed_cost[record.id] == pytest.approx(_hours_of(record, observed_at, hour_now()), rel=1e-6)

    # A late accrual (a run that read the record before it was regressed) is reversed next to the first entry.
    horizon = hour_now() + timedelta(hours=1)
    for record in records:
        await _accrue(repo, record, hour_now(), horizon)
    assert (await monitor.run(now=later + timedelta(hours=1)))["reversed"] == [change.change_id]
    assert (await monitor.run(now=later + timedelta(hours=2)))["reversed"] == []
    reversed_cost = await _reversed(repo, records)
    for record in records:
        assert reversed_cost[record.id] == pytest.approx(_hours_of(record, observed_at, horizon), rel=1e-6)
    events = [e for e in await scenario.changes.list_events(change.change_id) if e.event_type == "savings_reversed"]
    assert len(events) == 2
    first = {entry["recommendation_id"]: entry for entry in events[0].payload["entries"]}
    for entry in events[1].payload["entries"]:
        # Adjacent, not overlapping (a one-slot label is widened by one second).
        start = datetime.fromisoformat(entry["period_start"]) + timedelta(seconds=1)
        assert start == datetime.fromisoformat(first[entry["recommendation_id"]]["period_end"])


async def test_a_fresh_template_change_waits_for_its_grace(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=0)
    now = hour_now() + timedelta(minutes=30)
    await _observe(scenario, "digest-new", now - timedelta(minutes=20))
    result = await _monitor(scenario, _savings_repo(database)).run(now=now)
    assert result["closed"] == []
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    assert all(record_status(record) == "verified" for record in await scenario.records())


async def _observe_template(scenario: Scenario, observed_at: datetime, resources: str, image: str) -> None:
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid="uid-api",
                generation=8,
                spec_digest=f"digest-{resources}-{image}",
                resources_digest=resources,
                image_digest=image,
                observed_at=observed_at,
            )
        ]
    )


async def test_an_image_deployment_after_the_verdict_keeps_the_change_realized(database):  # noqa: F811
    """sim_specdrift day 12 (MEA-08): same requests, new image -> informational event, still accruing."""
    scenario, change, _, _ = await _realized(database, drift_days=0, digests=True)
    await _observe_template(scenario, hour_now() - timedelta(days=1), "res-1", "img-2")
    monitor = _monitor(scenario, _savings_repo(database))
    now = hour_now() + timedelta(minutes=30)
    assert (await monitor.run(now=now))["closed"] == []
    assert (await monitor.run(now=now + timedelta(hours=1)))["closed"] == []
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    assert all(record_status(record) == "verified" for record in await scenario.records())
    notes = [e for e in await scenario.changes.list_events(change.change_id) if e.event_type == "template_change"]
    assert len(notes) == 1 and notes[0].payload["image_changed"] is True


async def test_restoring_the_old_request_closes_the_change(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=0, digests=True)
    await _observe_template(scenario, hour_now() - timedelta(days=1), "res-0", "img-2")
    result = await _monitor(scenario, _savings_repo(database)).run(now=hour_now() + timedelta(minutes=30))
    assert result["closed"] == [change.change_id]
    assert (await scenario.changes.list_events(change.change_id))[-1].event_type == "spec_drift"
    assert all(record_status(record) == "regressed" for record in await scenario.records())


def _live(requests):
    async def read(namespace, kind, name):
        return requests

    return read


async def test_a_legacy_change_reads_the_live_requests_before_closing(database):  # noqa: F811
    """No resources digest: the applied requests still running (300m, 1 GiB) keep the change realized."""
    scenario, change, _, _ = await _realized(database, drift_days=0)
    await _observe(scenario, "digest-image-bump", hour_now() - timedelta(days=1))
    monitor = _monitor(scenario, _savings_repo(database))
    monitor.live_requests = _live({"api": (300, 1024**3)})
    assert (await monitor.run(now=hour_now() + timedelta(minutes=30)))["closed"] == []
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    notes = [e for e in await scenario.changes.list_events(change.change_id) if e.event_type == "template_change"]
    assert notes and notes[0].payload["resources_source"] == "live_requests"

    monitor.live_requests = _live({"api": (1000, 1024**3)})
    assert (await monitor.run(now=hour_now() + timedelta(minutes=45)))["closed"] == [change.change_id]
    event = (await scenario.changes.list_events(change.change_id))[-1]
    assert event.event_type == "spec_drift" and "live requests differ" in event.reason


async def test_an_older_change_recorded_late_does_not_supersede_a_newer_realized_one(database):  # noqa: F811
    """Review wp07-4 point 4: an adopted change that landed earlier never closes a later realized change."""
    from greenkube.models.metrics import RecommendationRecord, RecommendationType

    scenario, change, _, _ = await _realized(database, drift_days=0)
    await scenario.reco_repo.save_recommendations(
        [
            RecommendationRecord(
                type=RecommendationType.RIGHTSIZING_CPU,
                pod_name="api",
                namespace="prod",
                scope="workload",
                owner_kind="Deployment",
                owner_name="api",
                container_name="sidecar",
                description="oversized",
                current_cpu_request_millicores=500,
                recommended_cpu_request_millicores=200,
                potential_savings_cost=10.0,
            )
        ]
    )
    old = next(record for record in await scenario.records() if record.container_name == "sidecar")
    assert old.id is not None
    landed = change.rollout_completed_at - timedelta(days=3)
    applied = await scenario.lifecycle.apply(
        old.id, actual_cpu=200, application_method="manual", applied_at=landed, change_id="change-old"
    )
    await scenario.service.record_change("change-old", [applied], now=hour_now())
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
