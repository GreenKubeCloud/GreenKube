"""Drift monitor: workload deletion closes the measured accrual (roadmap C8 remainder).

A realized change whose workload is deleted stops accruing from the first
absent observation, and what accrued after it is reversed and projected into
the savings ledger v2 as a ``drift`` reversal. Runs on SQLite and, with
``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import timedelta
from typing import Dict, List, Optional

import pytest

from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.verify.drift import REVERSAL_SETTLE, SPEC_DRIFT_GRACE, DriftMonitor, DriftSettings
from greenkube.verify.ledger.identity import ClusterIdentityResolver, forget_cached_identities
from greenkube.verify.ledger.model import ENTRY_REVERSAL, REASON_DRIFT
from greenkube.verify.ledger.projector import LedgerProjector
from greenkube.verify.ledger.verification import verify_chain
from greenkube.verify.model import MeasurementState
from tests.integration.test_change_drift import (  # pyrefly: ignore[missing-import]
    CLUSTER,
    _accrue,
    _hours_of,
    _realized,
    _reversed,
    _savings_repo,
)
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]
from tests.verify.fixtures import hour_now, record_status  # pyrefly: ignore[missing-import]

UID = "kube-system-drift"


class Probe:
    """Answers of the Kubernetes API for the workload, one per pass."""

    def __init__(self, answers: List[Optional[bool]]) -> None:
        self.answers = list(answers)
        self.calls: List[tuple] = []

    async def __call__(self, namespace: str, kind: str, name: str) -> Optional[bool]:
        self.calls.append((namespace, kind, name))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _monitor(scenario, repo, probe: Probe, ledger=None) -> DriftMonitor:
    return DriftMonitor(
        scenario.service,
        savings_repo=repo,
        settings=DriftSettings(threshold=0.5, sustained_days=3),
        cluster_name=CLUSTER,
        assessed={},
        workload_probe=probe,
        ledger=ledger,
    )


def _projector(database) -> LedgerProjector:  # noqa: F811
    async def uid():
        return UID

    repo = LedgerRepository(database)
    return LedgerProjector(repo, ClusterIdentityResolver(repo, CLUSTER, reader=uid))


@pytest.fixture(autouse=True)
def _identities():
    forget_cached_identities()
    yield
    forget_cached_identities()


async def test_a_deleted_workload_closes_the_change_and_reverses_what_accrued_after(database):  # noqa: F811
    scenario, change, _, decided_at = await _realized(database, drift_days=0)
    repo = _savings_repo(database)
    records = await scenario.records()
    for record in records:
        await _accrue(repo, record, decided_at.replace(minute=0) + timedelta(hours=1), hour_now())
    projector = _projector(database)
    await projector.sync()
    probe = Probe([False])
    monitor = _monitor(scenario, repo, probe, ledger=projector)
    first_seen = hour_now() - timedelta(hours=5) + timedelta(minutes=10)

    # First absent observation: recorded, nothing closed yet.
    assert (await monitor.run(now=first_seen))["closed"] == []
    assert probe.calls == [("prod", "Deployment", "api")]
    events = await scenario.changes.list_events(change.change_id)
    assert events[-1].event_type == "workload_absent"
    # Still absent within the grace: nothing.
    assert (await monitor.run(now=first_seen + SPEC_DRIFT_GRACE / 2))["closed"] == []
    # Still absent after the grace: closed, accrual ends at the first absent observation.
    later = first_seen + SPEC_DRIFT_GRACE + timedelta(minutes=1)
    assert (await monitor.run(now=later))["closed"] == [change.change_id]
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CLOSED
    assert status.details["accrual_end"] == first_seen.isoformat()
    assert (await scenario.changes.list_events(change.change_id))[-1].event_type == "workload_deleted"
    assert all(record_status(record) == "regressed" for record in await scenario.records())

    # The accruals written after the deletion are reversed once, and become drift reversals in the v2 ledger.
    settled = later + REVERSAL_SETTLE + timedelta(minutes=1)
    assert (await monitor.run(now=settled))["reversed"] == [change.change_id]
    reversed_cost = await _reversed(repo, records)
    for record in records:
        assert reversed_cost[record.id] == pytest.approx(_hours_of(record, first_seen, hour_now()), rel=1e-6)
    ledger = LedgerRepository(database)
    drift: List = await ledger.list_entries(UID, filters={"reason": REASON_DRIFT}, limit=50)
    assert drift and all(entry.entry_kind == ENTRY_REVERSAL and entry.value < 0 for entry in drift)
    totals: Dict[int, float] = {}
    for entry in drift:
        assert entry.reverses_entry_id is not None
        totals[entry.recommendation_id or 0] = totals.get(entry.recommendation_id or 0, 0.0) + entry.value
    for record in records:
        assert -totals[record.id] == pytest.approx(reversed_cost[record.id], rel=1e-9)
    assert (await verify_chain(ledger, UID)).ok


async def test_a_workload_seen_again_is_not_closed(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=0)
    probe = Probe([False, True, True])
    monitor = _monitor(scenario, _savings_repo(database), probe)
    now = hour_now() + timedelta(minutes=5)
    await monitor.run(now=now)
    await monitor.run(now=now + timedelta(minutes=30))
    await monitor.run(now=now + SPEC_DRIFT_GRACE * 2)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    kinds = [event.event_type for event in await scenario.changes.list_events(change.change_id)]
    assert kinds[-2:] == ["workload_absent", "workload_present"]
    assert all(record_status(record) == "verified" for record in await scenario.records())


async def test_an_unknown_answer_decides_nothing(database):  # noqa: F811
    scenario, change, _, _ = await _realized(database, drift_days=0)
    monitor = _monitor(scenario, _savings_repo(database), Probe([None]))
    now = hour_now() + timedelta(minutes=5)
    for offset in (timedelta(0), SPEC_DRIFT_GRACE * 2):
        assert (await monitor.run(now=now + offset))["closed"] == []
    kinds = [event.event_type for event in await scenario.changes.list_events(change.change_id)]
    assert "workload_absent" not in kinds
