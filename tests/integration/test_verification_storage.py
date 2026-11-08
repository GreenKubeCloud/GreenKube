"""Real-database contract tests for verification storage (C0.11, C0.13; BUG-37).

Covers, on SQLite and PostgreSQL (``GREENKUBE_TEST_POSTGRES_DSN``):

* migration 0041 and the insert-only ``savings_measurements`` table;
* compare-and-swap lifecycle transitions written atomically with their event;
* a full verification cycle (measure, persist, re-measure) on real repositories.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.lifecycle import (
    RecommendationConflictError,
    RecommendationLifecycle,
    RecommendationTransitionError,
)
from greenkube.core.optimization.verifier import RecommendationVerifier
from greenkube.models.measurement import (
    MeasurementOutcome,
    QuantityEstimate,
    QuantityOutcome,
    SavingsMeasurement,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.verification import HealthGateResult, HealthGateStatus
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.core.optimization.verification_fixtures import (  # pyrefly: ignore[missing-import]
    WindowedMetricsRepo,
    rows,
)

WINDOW = timedelta(hours=6)
HOURS_PER_YEAR = 8766.0


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


def _recommendation_repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresRecommendationRepository(manager)
    return SQLiteRecommendationRepository(manager)


def _record(applied_at: datetime, **overrides) -> RecommendationRecord:
    values = dict(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="rightsize",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.APPLIED,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=500.0,
        potential_savings_co2e_grams=5000.0,
        applied_at=applied_at,
        application_method="pr_merge",
        baseline={"restart_count": 0},
        verification_status="pending",
    )
    values.update(overrides)
    return RecommendationRecord(**values)


async def _saved(repo, record: RecommendationRecord) -> RecommendationRecord:
    await repo.save_recommendations([record])
    stored = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    assert len(stored) == 1 and stored[0].id is not None
    return stored[0]


def _measurement(recommendation_id: int, sequence: int = 1, **overrides) -> SavingsMeasurement:
    values = dict(
        measurement_id=uuid4().hex,
        recommendation_id=recommendation_id,
        group_id=uuid4().hex,
        group_recommendation_ids=[recommendation_id],
        plan_id="plan-1",
        sequence=sequence,
        scope="workload",
        windows={"baseline": {"start": "a", "end": "b"}, "after": {"start": "b", "end": "c"}},
        estimates=[
            QuantityEstimate(
                quantity="cost",
                unit="currency",
                basis="total",
                before_per_hour=0.12,
                after_per_hour=0.096,
                delta_per_hour=0.024,
                annualized=0.024 * HOURS_PER_YEAR,
                claimed_annualized=0.024 * HOURS_PER_YEAR,
                outcome=QuantityOutcome.REALIZED,
            )
        ],
        health=[HealthGateResult(gate="oom_kills", status=HealthGateStatus.NOT_AVAILABLE, detail="no ksm")],
        ledger_input={"sample_count": 72},
        outcome=MeasurementOutcome.REALIZED,
        reasons=["note"],
        decided_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    )
    values.update(overrides)
    return SavingsMeasurement(**values).with_evidence_digest()


async def test_migration_creates_the_insert_only_measurement_table(database):
    repo = SavingsMeasurementRepository(database)
    first = _measurement(42)

    assert await repo.insert(first) == first
    assert await repo.get(first.measurement_id) == first
    assert await repo.get_for_cycle(42, 1) == first

    # A second decision for the same cycle is not written: the first one is replayed.
    assert await repo.insert(_measurement(42, outcome=MeasurementOutcome.NOT_REALIZED)) == first
    second = _measurement(42, sequence=2, supersedes_measurement_id=first.measurement_id)
    await repo.insert(second)
    assert [item.measurement_id for item in await repo.list_for_recommendation(42)] == [
        first.measurement_id,
        second.measurement_id,
    ]

    for statement in (
        "UPDATE savings_measurements SET outcome = 'not_realized'",
        "DELETE FROM savings_measurements",
    ):
        async with database.connection_scope() as conn:
            with pytest.raises(Exception, match="immutable"):
                await conn.execute(statement)
            if database.db_type == "sqlite":
                await conn.rollback()
    assert await repo.get(first.measurement_id) == first


async def test_transition_is_a_compare_and_swap_written_with_its_event(database):
    repo = _recommendation_repo(database)
    record = await _saved(repo, _record(datetime.now(timezone.utc), verification_status="failed"))
    assert record.id is not None
    events_before = len(await repo.get_events(record.id))

    missed = await repo.transition_and_record_event(
        record.id,
        {"status": "verifying"},
        "remeasure_requested",
        "user",
        {"reason": "stale read"},
        expected_statuses=["verified"],
    )
    assert missed is None
    unchanged = await repo.get_recommendation_by_id(record.id)
    assert unchanged is not None and unchanged.status is RecommendationStatus.APPLIED
    assert len(await repo.get_events(record.id)) == events_before

    lifecycle = RecommendationLifecycle(repo)
    remeasured = await lifecycle.request_remeasure(record.id, reason="new window", window_hours=6)
    assert remeasured.status is RecommendationStatus.VERIFYING
    assert remeasured.verification_status == "in_progress"
    events = await repo.get_events(record.id)
    assert events[-1].event_type == "remeasure_requested"
    assert len(events) == events_before + 1

    class StaleRepo:
        """Returns the pre-transition record, as a concurrent reader would have seen it."""

        def __init__(self, inner, stale):
            self.inner, self.stale = inner, stale

        async def get_recommendation_by_id(self, rec_id):
            return self.stale

        async def transition_and_record_event(self, *args, **kwargs):
            return await self.inner.transition_and_record_event(*args, **kwargs)

    with pytest.raises(RecommendationConflictError):
        await RecommendationLifecycle(StaleRepo(repo, record)).mark_inconclusive(record.id, reason="race")  # type: ignore[arg-type]
    assert len(await repo.get_events(record.id)) == events_before + 1


async def test_verification_cycle_persists_and_remeasures_on_real_repositories(database):
    repo = _recommendation_repo(database)
    measurements = SavingsMeasurementRepository(database)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    applied_at = now - timedelta(hours=8)
    record = await _saved(repo, _record(applied_at))
    metrics = WindowedMetricsRepo(
        rows(applied_at - WINDOW, now, cost=0.01) + rows(now, now + timedelta(hours=8), cost=0.008)
    )
    cfg = Config(VERIFICATION_WINDOW_HOURS=6, VERIFICATION_MIN_SAMPLES=12)

    def verifier(clock=None) -> RecommendationVerifier:
        return RecommendationVerifier(
            RecommendationLifecycle(repo),
            metrics,  # pyrefly: ignore[bad-argument-type]
            config=cfg,
            measurement_repo=measurements,
            clock=clock,
        )

    first = (await verifier().verify_due())[0]
    assert first.status is RecommendationStatus.APPLIED
    assert first.verification_status == "failed"

    later = verifier(clock=lambda: now + timedelta(hours=8))
    assert await later.verify_due() == []

    await RecommendationLifecycle(repo).request_remeasure(record.id, reason="retry", window_hours=6)  # type: ignore[arg-type]
    second = (await later.verify_due())[0]

    assert second.status is RecommendationStatus.VERIFIED
    assert second.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
    assert second.measured_co2e_saved_grams == 0.0
    history = await measurements.list_for_recommendation(record.id)  # type: ignore[arg-type]
    assert [item.outcome for item in history] == [MeasurementOutcome.NOT_REALIZED, MeasurementOutcome.REALIZED]
    assert history[1].supersedes_measurement_id == history[0].measurement_id
    baseline = second.baseline.model_dump()  # type: ignore[union-attr]
    assert baseline["measurement_ids"] == [item.measurement_id for item in history]
    assert baseline["measured_ledger_input"]["measurement_id"] == history[1].measurement_id
    event_types = [event.event_type for event in await repo.get_events(record.id)]  # type: ignore[arg-type]
    assert event_types[-3:] == ["not_realized", "remeasure_requested", "verified"]


async def _stored_by_pod(repo) -> dict[str, RecommendationRecord]:
    stored = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    return {record.pod_name: record for record in stored}


async def test_ignore_unignore_and_expiry_are_compare_and_swaps_with_their_events(database):
    repo = _recommendation_repo(database)
    now = datetime.now(timezone.utc)
    await repo.save_recommendations(
        [
            _record(now, pod_name="old", status=RecommendationStatus.ACTIVE, expires_at=now - WINDOW).model_copy(
                update={"applied_at": None, "verification_status": None, "application_method": None}
            ),
            _record(now, pod_name="landed", owner_name="landed", verification_status="pending"),
        ]
    )
    records = await _stored_by_pod(repo)
    old_id, landed_id = records["old"].id, records["landed"].id
    assert old_id is not None and landed_id is not None
    lifecycle = RecommendationLifecycle(repo)

    expired = await lifecycle.expire()
    assert [record.id for record in expired] == [old_id]
    ignored = await lifecycle.ignore(old_id, reason="kept warm")
    restored = await lifecycle.unignore(old_id)

    assert ignored.status is RecommendationStatus.IGNORED and ignored.ignored_reason == "kept warm"
    assert restored.status is RecommendationStatus.ACTIVE and restored.ignored_reason is None
    assert [event.event_type for event in await repo.get_events(old_id)] == ["expired", "ignored", "unignored"]
    with pytest.raises(RecommendationTransitionError):
        await lifecycle.ignore(landed_id, reason="noise")
    with pytest.raises(RecommendationTransitionError):
        await lifecycle.unignore(landed_id)
    landed = await repo.get_recommendation_by_id(landed_id)
    assert landed is not None and landed.status is RecommendationStatus.APPLIED
    assert await repo.get_events(landed_id) == []


async def test_remeasured_attribution_group_claims_the_delta_once(database):
    """BUG-36 through re-measurement: CPU and memory of one plan, re-measured together."""
    repo = _recommendation_repo(database)
    measurements = SavingsMeasurementRepository(database)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    applied_at = now - timedelta(hours=30)
    shared = dict(
        verification_status="inconclusive",
        baseline={"restart_count": 0, "plan_id": "plan-1", "baseline_window_end": applied_at.isoformat()},
        recommended_memory_request_bytes=512 * 1024**2,
    )
    await repo.save_recommendations(
        [
            _record(applied_at, potential_savings_cost=300.0, **shared),
            _record(applied_at, type=RecommendationType.RIGHTSIZING_MEMORY, potential_savings_cost=100.0, **shared),
        ]
    )
    stored = await repo.get_recommendations_by_statuses(["applied"])
    cpu_id = next(record.id for record in stored if record.type is RecommendationType.RIGHTSIZING_CPU)
    memory_id = next(record.id for record in stored if record.type is RecommendationType.RIGHTSIZING_MEMORY)
    assert cpu_id is not None and memory_id is not None

    await RecommendationLifecycle(repo).request_remeasure(cpu_id, window_hours=6, now=now)
    memory = await repo.get_recommendation_by_id(memory_id)
    assert memory is not None and memory.status is RecommendationStatus.VERIFYING

    clock = now + timedelta(hours=7)
    metrics = WindowedMetricsRepo(
        rows(applied_at - WINDOW, applied_at, cost=0.01, memory=200 * 1024**2)
        + rows(applied_at, clock, cost=0.007, memory=200 * 1024**2)
    )
    updated = await RecommendationVerifier(
        RecommendationLifecycle(repo),
        metrics,  # pyrefly: ignore[bad-argument-type]
        config=Config(VERIFICATION_WINDOW_HOURS=6, VERIFICATION_MIN_SAMPLES=12),
        measurement_repo=measurements,
        clock=lambda: clock,
    ).verify_due()

    assert {record.status for record in updated} == {RecommendationStatus.VERIFIED}
    assert sum(record.measured_cost_saved or 0.0 for record in updated) == pytest.approx(0.003 * 12 * HOURS_PER_YEAR)
    for record_id in (cpu_id, memory_id):
        latest = (await measurements.list_for_recommendation(record_id))[-1]
        assert latest.group_recommendation_ids == sorted([cpu_id, memory_id])
