"""Change measurement on real databases (C1, C2, C4, C5).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL:

* migrations 0056/0057 and the insert-only change tables;
* compare-and-swap lifecycle transitions with their events;
* the full pipeline: record a change, freeze its baseline, measure it, split it
  across its recommendations and drive the recommendation lifecycle;
* re-measurement, contamination, interim health review and supersession.
"""

import os
from datetime import timedelta
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.change_repository import ChangeConflictError, ChangeRepository
from greenkube.verify.model import (
    ChangeMeasurement,
    ChangeOutcome,
    MeasurementState,
    TimeWindow,
    WindowPlan,
    WorkloadSpecObservation,
)
from greenkube.verify.service import DECISION_GRACE
from greenkube.verify.strategy import HOURS_PER_YEAR
from tests.verify.fixtures import (  # pyrefly: ignore[missing-import]
    Scenario,
    change_event,
    hour_now,
    record_status,
)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[DatabaseManager, None]:
    """An isolated, fully migrated database per backend."""
    backend = request.param
    postgres_dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run real PostgreSQL integration tests.")
        schema = f"gk_test_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "greenkube.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if postgres_dsn and schema:
            conn = await asyncpg.connect(postgres_dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def _execute(manager: DatabaseManager, sql: str) -> None:
    async with manager.connection_scope() as conn:
        await conn.execute(sql)
        if manager.db_type != "postgres":
            await conn.commit()


# ----------------------------------------------------------------------
# Repository contract
# ----------------------------------------------------------------------


async def test_change_tables_are_insert_only_and_transitions_compare_and_swap(database):
    repo = ChangeRepository(database)
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed, recommendation_ids=[1])
    stored, created = await repo.insert_change(change)
    assert created and stored.recommendation_ids == [1]
    again, created_again = await repo.insert_change(
        change.model_copy(update={"change_id": "other", "recommendation_ids": [2]})
    )
    assert not created_again and again.change_id == change.change_id and again.recommendation_ids == [1, 2]
    assert await repo.find_change_id("missing") is None

    with pytest.raises(Exception, match="immutable"):
        await _execute(database, "UPDATE change_events SET pr_url = 'x'")
    with pytest.raises(Exception, match="immutable"):
        await _execute(database, "DELETE FROM change_members")

    status = await repo.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.APPLIED and status.version == 1
    measuring = await repo.transition(
        status, MeasurementState.MEASURING, event_type="measurement_started", payload={"x": 1}
    )
    assert measuring.version == 2
    with pytest.raises(ChangeConflictError):
        await repo.transition(status, MeasurementState.CLOSED, event_type="stale")
    events = await repo.list_events(change.change_id)
    assert [(event.event_type, event.from_state, event.to_state) for event in events] == [
        ("change_recorded", None, MeasurementState.APPLIED),
        ("measurement_started", MeasurementState.APPLIED, MeasurementState.MEASURING),
    ]
    with pytest.raises(Exception, match="immutable"):
        await _execute(database, "DELETE FROM change_state_events")

    plan = WindowPlan(
        pre=TimeWindow(start=completed - timedelta(days=7), end=completed),
        post=TimeWindow(start=completed, end=completed + timedelta(days=7)),
        warmup_hours=1,
        min_coverage=0.8,
    )
    measurement = ChangeMeasurement(
        measurement_id="m1",
        change_id=change.change_id,
        strategy_key="request_rightsizing",
        strategy_version="1.0",
        windows=plan,
        outcome=ChangeOutcome.REALIZED,
        decided_at=completed + timedelta(days=8),
    ).with_evidence_digest()
    saved, new = await repo.insert_measurement(measurement)
    assert new and saved == measurement
    replay, new_again = await repo.insert_measurement(measurement.model_copy(update={"measurement_id": "m2"}))
    assert not new_again and replay.measurement_id == "m1"
    with pytest.raises(Exception, match="immutable"):
        await _execute(database, "UPDATE change_measurements SET outcome = 'not_realized'")

    rows = await repo.list_changes(states=["measuring"], namespace="prod")
    assert [(item.change_id, state.state) for item, state in rows] == [(change.change_id, MeasurementState.MEASURING)]
    assert await repo.list_changes(states=["realized"]) == []
    assert await repo.change_ids_for_recommendations([1, 2, 3]) == {1: change.change_id, 2: change.change_id}


async def test_change_listing_pages_newest_first(database):
    repo = ChangeRepository(database)
    base = hour_now() - timedelta(days=20)
    for index in range(5):
        await repo.insert_change(
            change_event(base + timedelta(days=index), change_id=f"c{index}", generation_after=index + 2)
        )
    first = await repo.list_changes(limit=2)
    assert [change.change_id for change, _ in first] == ["c4", "c3"]
    cursor = (first[-1][0].created_at, first[-1][0].change_id)
    second = await repo.list_changes(limit=2, before=cursor)
    assert [change.change_id for change, _ in second] == ["c2", "c1"]


async def test_spec_observations_keep_the_latest_revision(database):
    repo = ChangeRepository(database)
    start = hour_now() - timedelta(days=3)
    observations = [
        WorkloadSpecObservation(
            namespace="prod",
            kind="Deployment",
            name="api",
            generation=g,
            spec_digest=f"d{g}",
            observed_at=start + timedelta(hours=g),
        )
        for g in (1, 2, 3)
    ]
    assert await repo.insert_spec_observations(observations) == 3
    latest = await repo.latest_spec_observations()
    assert latest[("prod", "Deployment", "api")].generation == 3
    since = await repo.spec_observations("prod", "Deployment", "api", since=start + timedelta(hours=2, minutes=30))
    assert [item.generation for item in since] == [2, 3]


# ----------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------


async def test_a_change_is_measured_once_and_split_across_its_recommendations(database):
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, applied = await scenario.apply()
    cpu_id, memory_id = (record.id for record in applied)

    # applied -> measuring: the post-window starts after the warm-up and is shown on the recommendations.
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.MEASURING
    assert status.post_window_start == completed + timedelta(hours=1)
    assert status.post_window_end == completed + timedelta(hours=1, days=7)
    for record in await scenario.records():
        assert record_status(record) == "verifying" and record.verification_status == "in_progress"
        assert record.verification_window_end == status.post_window_end

    # The interim verifier leaves change-managed recommendations alone.
    assert await scenario.verifier.verify_due() == []

    # Interim health check after VERIFICATION_WINDOW_HOURS: healthy, nothing flagged.
    interim = status.post_window_start + timedelta(hours=72) + DECISION_GRACE
    assert await scenario.service.run_due(now=interim) == []
    status = await scenario.changes.get_status(change.change_id)
    assert (
        status is not None and status.details["health_checked"] is True and status.state is MeasurementState.MEASURING
    )

    # Nothing is decided before the post-window (plus grace) ends.
    assert await scenario.service.run_due(now=status.post_window_end) == []
    updated = await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    assert sorted(record.id for record in updated) == [cpu_id, memory_id]

    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED
    measurements = await scenario.changes.list_measurements(change.change_id)
    assert len(measurements) == 1
    measurement = measurements[0]
    assert measurement.measurement_id == status.latest_measurement_id
    assert measurement.outcome is ChangeOutcome.REALIZED and measurement.evidence_sha256
    assert measurement.load_driver is not None and measurement.load_driver.key == "cpu_usage"
    assert measurement.confounders == []
    snapshot = await scenario.changes.get_snapshot_for_change(change.change_id)
    assert snapshot is not None and measurement.baseline_sha256 == snapshot.content_sha256

    cost = measurement.estimate("allocated_cost")
    assert cost is not None and cost.ci_low is not None and cost.ci_low > 0
    cpu_saving = 2 * 0.7 * 0.08  # 700m x 2 replicas
    ram_saving = 2 * 1.0 * 0.01  # 1 GiB x 2 replicas
    assert cost.rate_per_hour == pytest.approx(cpu_saving + ram_saving, rel=1e-6)
    shares = {allocation.recommendation_id: allocation for allocation in measurement.allocations}
    assert shares[cpu_id].quantity == "cost_cpu" and shares[memory_id].quantity == "cost_ram"

    records = {record.id: record for record in await scenario.records()}
    assert record_status(records[cpu_id]) == "verified" and record_status(records[memory_id]) == "verified"
    assert records[cpu_id].measured_cost_saved == pytest.approx(cpu_saving * HOURS_PER_YEAR, rel=1e-6)
    assert records[memory_id].measured_cost_saved == pytest.approx(ram_saving * HOURS_PER_YEAR, rel=1e-6)
    assert records[cpu_id].measured_co2e_saved_grams == 0.0

    rows = await scenario.measurements.list_for_change(change.change_id)
    assert sorted(row.recommendation_id for row in rows) == [cpu_id, memory_id]
    assert {row.group_id for row in rows} == {measurement.measurement_id}
    assert all(row.strategy_key == "request_rightsizing" and row.outcome.value == "realized" for row in rows)
    assert all(row.ledger_input["measurement_id"] == row.measurement_id for row in rows)

    # Decided changes stay decided; the track record counts each recommendation once.
    assert await scenario.service.run_due(now=status.post_window_end + timedelta(hours=5)) == []
    counts = await scenario.measurements.get_outcome_counts_by_source_capability()
    assert sum(entry["realized"] for entry in counts.values()) == 2
    events = [event.event_type for event in await scenario.changes.list_events(change.change_id)]
    assert events == ["change_recorded", "measurement_started", "health_checked", "measurement_decided"]


async def test_not_realized_change_is_remeasured_only_on_request(database):
    scenario = Scenario(database)
    completed = scenario.completed
    # Requests changed but the cost did not move (e.g. usage above the new request).
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed + timedelta(days=9)))
    change, applied = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.NOT_REALIZED
    records = await scenario.records()
    assert all(record_status(record) == "applied" and record.verification_status == "failed" for record in records)
    assert all(record.measured_cost_saved == pytest.approx(0.0, abs=1e-9) for record in records)

    # Fixed horizon: no automatic re-test.
    later = status.post_window_end + timedelta(days=1)
    assert await scenario.service.run_due(now=later) == []
    assert (await scenario.changes.get_status(change.change_id)).state is MeasurementState.NOT_REALIZED

    assert applied[0].id is not None
    await scenario.lifecycle.request_remeasure(applied[0].id, reason="usage dropped", now=later)
    await scenario.service.run_due(now=later)
    remeasuring = await scenario.changes.get_status(change.change_id)
    assert remeasuring is not None and remeasuring.state is MeasurementState.MEASURING
    assert remeasuring.sequence == 2
    assert remeasuring.post_window_start is not None and remeasuring.post_window_start > later
    events = [event.event_type for event in await scenario.changes.list_events(change.change_id)]
    assert events[-1] == "remeasure_started"


async def test_a_later_change_on_the_workload_contaminates_the_measurement(database):
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, _ = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))

    # Another rollout on the same workload two days later (e.g. an image bump).
    second = change_event(
        completed + timedelta(days=2), change_id="change-b", recommendation_ids=[], generation_after=3
    )
    await scenario.changes.insert_change(second)

    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.CONTAMINATED
    measurement = (await scenario.changes.list_measurements(change.change_id))[0]
    assert measurement.confidence == 0.0
    assert [item.kind for item in measurement.confounders] == ["overlapping_change"]
    records = await scenario.records()
    assert all(record.verification_status == "inconclusive" for record in records)
    rows = await scenario.measurements.list_for_change(change.change_id)
    assert all(row.reasons and row.reasons[0].startswith("contaminated") for row in rows)


async def test_interim_health_failure_sends_the_change_to_rollback_review(database):
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    # Per-pod usage 400m exceeds the applied 300m request x 1.1.
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)),
        cpu_request=300,
        memory_request=1024**3,
        cpu_usage=400,
    )
    change, _ = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_start is not None and status.post_window_end is not None
    flagged = await scenario.service.run_due(now=status.post_window_start + timedelta(hours=72) + DECISION_GRACE)
    assert {record_status(record) for record in flagged} == {"rollback_review"}
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.details["health_failed"] is True
    assert status.state is MeasurementState.MEASURING

    # The change is still measured (its savings are evidence); flagged recommendations are left alone.
    assert await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE) == []
    assert (await scenario.changes.get_status(change.change_id)).state is MeasurementState.REALIZED
    assert all(record_status(record) == "rollback_review" for record in await scenario.records())
    assert await scenario.measurements.list_for_change(change.change_id) == []
    # The decisioning track record counts the review as a rollback of the source's recommendations.
    counts = await scenario.measurements.get_outcome_counts_by_source_capability()
    assert sum(entry["rollback"] for entry in counts.values()) == 2
    assert sum(entry["realized"] for entry in counts.values()) == 0


async def test_two_replicas_under_the_new_request_are_healthy(database):
    """Audit scenario: 2 replicas at 200m each with 300m applied must not go to rollback review (BUG-28)."""
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, _ = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    measurement = (await scenario.changes.list_measurements(change.change_id))[0]
    usage = [gate for gate in measurement.health if gate.gate in {"cpu_p95", "memory_p95"}]
    assert usage and all(gate.status.value == "passed" for gate in usage)
    assert all(record_status(record) == "verified" for record in await scenario.records())


async def test_a_new_change_closes_the_realized_one(database):
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, _ = await scenario.apply()
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    assert (await scenario.changes.get_status(change.change_id)).state is MeasurementState.REALIZED

    later = completed + timedelta(days=9)
    await scenario.apply(change_id="change-c", completed=later, generation=3)
    closed = await scenario.changes.get_status(change.change_id)
    assert closed is not None and closed.state is MeasurementState.CLOSED
    events = await scenario.changes.list_events(change.change_id)
    assert events[-1].event_type == "superseded" and events[-1].payload["superseded_by"] == "change-c"


async def test_an_unrecorded_change_is_repaired_from_its_recommendations(database):
    scenario = Scenario(database)
    completed = scenario.completed
    await scenario.reco_repo.save_recommendations(
        [
            RecommendationRecord(
                pod_name="api",
                namespace="prod",
                scope="workload",
                owner_kind="Deployment",
                owner_name="api",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="oversized",
                current_cpu_request_millicores=1000,
                recommended_cpu_request_millicores=300,
            )
        ]
    )
    record = (await scenario.records())[0]
    assert record.id is not None
    await scenario.lifecycle.apply(record.id, actual_cpu=300, applied_at=completed, change_id="lost-change")
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    change = await scenario.changes.get_change("lost-change")
    assert change is not None and change.recommendation_ids == [record.id]
    status = await scenario.changes.get_status("lost-change")
    assert status is not None and status.state is MeasurementState.MEASURING
