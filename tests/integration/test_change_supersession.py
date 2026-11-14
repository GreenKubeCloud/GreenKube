"""Superseded changes keep being watched (C8, production-readiness BLOCKER).

A realized change closed as ``superseded`` by a newer change on the same
workload keeps accruing its own measured step. Before this fix nothing ever
ended that accrual: the drift monitor only watched realized changes, so a
superseded change accrued forever, even after its workload was deleted. Runs
on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.verify.drift import REVERSAL_SETTLE, SPEC_DRIFT_GRACE, DriftMonitor, DriftSettings
from greenkube.verify.model import MeasurementState, WorkloadSpecObservation
from tests.integration.test_change_drift import (  # pyrefly: ignore[missing-import]
    CLUSTER,
    _accrue,
    _hours_of,
    _realized,
    _reversed,
    _savings_repo,
)
from tests.integration.test_change_drift_deletion import Probe  # pyrefly: ignore[missing-import]
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import hour_now, record_status  # pyrefly: ignore[missing-import]


def _monitor(scenario, repo, probe=None) -> DriftMonitor:
    return DriftMonitor(
        scenario.service,
        savings_repo=repo,
        settings=DriftSettings(threshold=0.5, sustained_days=3),
        cluster_name=CLUSTER,
        assessed={},
        workload_probe=probe,
    )


async def _superseded(database):  # noqa: F811
    """Change A realized and accruing, then change B (not measured yet) lands on the same workload."""
    scenario, change, _, decided_at = await _realized(database, drift_days=0)
    repo = _savings_repo(database)
    first = await scenario.records()
    for record in first:
        await _accrue(repo, record, decided_at.replace(minute=0) + timedelta(hours=1), hour_now())
    newer, _ = await scenario.apply(change_id="change-b", completed=hour_now() - timedelta(days=2), generation=3)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CLOSED
    # Superseded, A still accrues its own measured step.
    assert all(record_status(record) == "verified" for record in first)
    return scenario, change, newer, first, repo


async def test_a_superseded_change_stops_accruing_when_the_workload_is_deleted(database):  # noqa: F811
    scenario, change, newer, first, repo = await _superseded(database)
    probe = Probe([False])
    monitor = _monitor(scenario, repo, probe)
    first_seen = hour_now() - timedelta(hours=5) + timedelta(minutes=10)

    assert (await monitor.run(now=first_seen))["closed"] == []
    later = first_seen + SPEC_DRIFT_GRACE + timedelta(minutes=1)
    assert change.change_id in (await monitor.run(now=later))["closed"]

    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CLOSED
    assert status.details["accrual_end"] == first_seen.isoformat()
    assert (await scenario.changes.list_events(change.change_id))[-1].event_type == "workload_deleted"
    by_id = {record.id: record for record in await scenario.records()}
    assert all(record_status(by_id[record.id]) == "regressed" for record in first)

    # What A accrued after the deletion is reversed (dev kept accruing it forever).
    settled = later + REVERSAL_SETTLE + timedelta(minutes=1)
    assert change.change_id in (await monitor.run(now=settled))["reversed"]
    reversed_cost = await _reversed(repo, first)
    for record in first:
        assert reversed_cost[record.id] == pytest.approx(_hours_of(record, first_seen, hour_now()), rel=1e-6)
    # Ended once: later passes do not touch it again.
    assert change.change_id not in (await monitor.run(now=settled + timedelta(hours=1)))["closed"]


async def test_a_superseded_change_ends_with_the_newer_change_that_ended(database):  # noqa: F811
    scenario, change, newer, first, repo = await _superseded(database)
    ended_at = hour_now() - timedelta(hours=20)
    head = await scenario.changes.get_status(newer.change_id)
    assert head is not None
    # The newer change's saving ended (e.g. its workload was deleted or it was reverted).
    await scenario.changes.transition(
        head,
        MeasurementState.CLOSED,
        event_type="workload_deleted",
        reason="the workload was deleted",
        details={"accrual_end": ended_at.isoformat(), "accrual_end_reason": "the workload was deleted"},
    )
    result = await _monitor(scenario, repo).run(now=hour_now() + timedelta(minutes=5))

    assert change.change_id in result["closed"]
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.details["accrual_end"] == ended_at.isoformat()
    event = (await scenario.changes.list_events(change.change_id))[-1]
    assert event.event_type == "superseded_ended" and event.payload["ended_by"] == newer.change_id
    by_id = {record.id: record for record in await scenario.records()}
    assert all(record_status(by_id[record.id]) == "regressed" for record in first)


async def test_a_superseded_change_ends_on_a_spec_drift_of_the_newest_change(database):  # noqa: F811
    scenario, change, newer, first, repo = await _superseded(database)
    observed_at = hour_now() - timedelta(hours=10) + timedelta(minutes=7)
    await scenario.changes.insert_spec_observations(
        [
            WorkloadSpecObservation(
                namespace="prod",
                kind="Deployment",
                name="api",
                workload_uid="uid-api",
                generation=9,
                spec_digest="digest-hand-edit",
                observed_at=observed_at,
            )
        ]
    )
    result = await _monitor(scenario, repo).run(now=datetime.now(timezone.utc))

    assert change.change_id in result["closed"]
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.details["accrual_end"] == observed_at.isoformat()
    by_id = {record.id: record for record in await scenario.records()}
    assert all(record_status(by_id[record.id]) == "regressed" for record in first)


async def test_a_superseded_change_keeps_accruing_while_nothing_ended(database):  # noqa: F811
    scenario, change, _, first, repo = await _superseded(database)
    result = await _monitor(scenario, repo, Probe([True])).run(now=hour_now() + timedelta(minutes=5))
    assert change.change_id not in result["closed"]
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and "accrual_end" not in status.details
    by_id = {record.id: record for record in await scenario.records()}
    assert all(record_status(by_id[record.id]) == "verified" for record in first)
