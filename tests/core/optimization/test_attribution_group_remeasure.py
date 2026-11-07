# tests/core/optimization/test_attribution_group_remeasure.py
"""BUG-36 must not come back through re-measurement or rollback-review dismissal (C0.10).

The CPU and memory records of one change share one workload cost delta. When
attribution groups were only formed among the records awaiting measurement in
one pass, and only when their measurement windows started within 15 minutes,
re-measuring (or dismissing) the two records at different times, or only one
of them, measured each record alone: each claimed the whole delta.

Group membership is now sticky and the share of each record is computed
against every member of its change that is still in effect.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle, RecommendationTransitionError
from greenkube.core.optimization.verifier import RecommendationVerifier, attribution_groups
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

from .verification_fixtures import WindowedMetricsRepo, baseline_of, rec_id, rows  # pyrefly: ignore[missing-import]

HOURS_PER_YEAR = 8766.0
WINDOW = timedelta(hours=6)
BEFORE_COST, AFTER_COST = 0.01, 0.007
#: Annual saving of the whole workload: the most the CPU and memory records may claim together.
WORKLOAD_DELTA = (BEFORE_COST - AFTER_COST) * 12 * HOURS_PER_YEAR
MEMORY = 200 * 1024**2


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


def _config() -> Config:
    cfg = Config()
    cfg.VERIFICATION_WINDOW_HOURS = 6
    cfg.VERIFICATION_MIN_SAMPLES = 12
    return cfg


def _record(applied_at: datetime, rtype: RecommendationType, potential: float, **overrides) -> RecommendationRecord:
    values = dict(
        pod_name="api",
        namespace="prod",
        type=rtype,
        description="rightsize",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.APPLIED,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        current_memory_request_bytes=1024**3,
        recommended_memory_request_bytes=512 * 1024**2,
        potential_savings_cost=potential,
        potential_savings_co2e_grams=1000.0,
        applied_at=applied_at,
        application_method="pr_merge",
        baseline={"restart_count": 0, "plan_id": "plan-1", "baseline_window_end": applied_at.isoformat()},
        verification_status="pending",
    )
    values.update(overrides)
    return RecommendationRecord(**values)


async def _pair(repo, applied_at: datetime, *, memory_applied_at=None, **overrides) -> tuple[int, int]:
    await repo.save_recommendations(
        [
            _record(applied_at, RecommendationType.RIGHTSIZING_CPU, 300.0, **overrides),
            _record(memory_applied_at or applied_at, RecommendationType.RIGHTSIZING_MEMORY, 100.0, **overrides),
        ]
    )
    records = await _all(repo)
    cpu = next(record for record in records if record.type is RecommendationType.RIGHTSIZING_CPU)
    memory = next(record for record in records if record.type is RecommendationType.RIGHTSIZING_MEMORY)
    return rec_id(cpu), rec_id(memory)


async def _all(repo) -> list[RecommendationRecord]:
    records = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    return sorted(records, key=lambda record: record.id or 0)


async def _get(repo, record_id: int) -> RecommendationRecord:
    record = await repo.get_recommendation_by_id(record_id)
    assert record is not None
    return record


def _metrics(applied_at: datetime, end: datetime) -> WindowedMetricsRepo:
    return WindowedMetricsRepo(
        rows(applied_at - WINDOW - timedelta(hours=12), applied_at, cost=BEFORE_COST, memory=MEMORY)
        + rows(applied_at, end, cost=AFTER_COST, memory=MEMORY)
    )


def _verifier(repo, metrics, clock) -> RecommendationVerifier:
    return RecommendationVerifier(
        RecommendationLifecycle(repo),
        metrics,
        config=_config(),
        clock=lambda: clock,
    )


def _claimed(records) -> float:
    return sum(record.measured_cost_saved or 0.0 for record in records)


class TestRemeasure:
    async def test_members_remeasured_an_hour_apart_claim_the_delta_once(self, repo):
        """Reviewer probe: both members are re-measured, their windows start an hour apart."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=30)
        cpu_id, memory_id = await _pair(repo, applied_at, verification_status="inconclusive")
        start = now - timedelta(hours=9)
        for record_id, offset in ((cpu_id, timedelta(0)), (memory_id, timedelta(hours=1))):
            record = await _get(repo, record_id)
            baseline = baseline_of(record) | {"measurement_sequence": 2}
            await repo.update_recommendation_fields(
                record_id,
                {
                    "status": "verifying",
                    "verification_status": "in_progress",
                    "verification_window_start": start + offset,
                    "verification_window_end": start + offset + WINDOW,
                    "baseline": baseline,
                },
            )

        updated = await _verifier(repo, _metrics(applied_at, now), now).verify_due()

        assert {record.status for record in updated} == {RecommendationStatus.VERIFIED}
        assert len(updated) == 2
        assert _claimed(updated) == pytest.approx(WORKLOAD_DELTA)

    async def test_remeasuring_one_member_while_its_sibling_keeps_its_claim(self, repo):
        """The CPU record keeps its realized share; only the memory record is re-measured."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=30)
        cpu_id, memory_id = await _pair(repo, applied_at)
        await repo.update_recommendation_fields(
            cpu_id,
            {
                "status": "verified",
                "verification_status": "passed",
                "measured_cost_saved": WORKLOAD_DELTA * 0.75,
                "savings_realized": True,
            },
        )
        await repo.update_recommendation_fields(memory_id, {"verification_status": "inconclusive"})

        remeasured = await RecommendationLifecycle(repo).request_remeasure(memory_id, window_hours=6)
        assert remeasured.status is RecommendationStatus.VERIFYING
        assert (await _get(repo, cpu_id)).status is RecommendationStatus.VERIFIED

        clock = now + timedelta(hours=7)
        updated = await _verifier(repo, _metrics(applied_at, clock), clock).verify_due()

        assert [rec_id(record) for record in updated] == [memory_id]
        memory = updated[0]
        assert memory.status is RecommendationStatus.VERIFIED
        assert memory.measured_cost_saved == pytest.approx(WORKLOAD_DELTA * 0.25)
        cpu = await _get(repo, cpu_id)
        assert _claimed([cpu, memory]) == pytest.approx(WORKLOAD_DELTA)

    async def test_remeasure_starts_one_window_for_the_whole_group(self, repo):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=30)
        cpu_id, memory_id = await _pair(repo, applied_at, verification_status="inconclusive")
        lifecycle = RecommendationLifecycle(repo)

        await lifecycle.request_remeasure(cpu_id, reason="traffic spike", window_hours=6, now=now)
        # Asking again for the sibling an hour later is refused: it is already being re-measured.
        with pytest.raises(RecommendationTransitionError):
            await lifecycle.request_remeasure(memory_id, window_hours=6, now=now + timedelta(hours=1))

        cpu, memory = await _get(repo, cpu_id), await _get(repo, memory_id)
        assert cpu.status is memory.status is RecommendationStatus.VERIFYING
        assert cpu.verification_window_start == memory.verification_window_start == now
        assert cpu.verification_window_end == memory.verification_window_end == now + WINDOW
        sibling_event = (await repo.get_events(memory_id))[-1]
        payload = sibling_event.payload.model_dump()
        assert sibling_event.event_type == "remeasure_requested"
        assert payload["requested_via"] == cpu_id
        assert payload["attribution_group_ids"] == [cpu_id, memory_id]

        clock = now + timedelta(hours=7)
        updated = await _verifier(repo, _metrics(applied_at, clock), clock).verify_due()

        assert len(updated) == 2
        assert _claimed(updated) == pytest.approx(WORKLOAD_DELTA)

    async def test_membership_recorded_by_a_measurement_is_sticky(self, repo):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=8)
        cpu_id, memory_id = await _pair(repo, applied_at)

        updated = await _verifier(repo, _metrics(applied_at, now), now).verify_due()

        assert _claimed(updated) == pytest.approx(WORKLOAD_DELTA)
        measurements = SavingsMeasurementRepository(db_manager)
        for record in updated:
            assert baseline_of(record)["attribution_group_ids"] == [cpu_id, memory_id]
            stored = await measurements.list_for_recommendation(rec_id(record))
            assert stored[0].group_recommendation_ids == [cpu_id, memory_id]
        # The stored membership wins over the heuristics (here: a plan id edited away).
        records = await _all(repo)
        edited = [
            record.model_copy(update={"baseline": baseline_of(record) | {"plan_id": f"plan-{rec_id(record)}"}})
            for record in records
        ]
        assert [sorted(rec_id(member) for member in group) for group in attribution_groups(edited)] == [
            [cpu_id, memory_id]
        ]


class TestDismiss:
    async def test_dismissing_one_member_dismisses_the_change(self, repo):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=30)
        cpu_id, memory_id = await _pair(
            repo, applied_at, status=RecommendationStatus.ROLLBACK_REVIEW, verification_status="failed"
        )

        await RecommendationLifecycle(repo).dismiss_rollback_review(
            cpu_id, reason="restarts caused by a node drain", window_hours=6, now=now
        )

        cpu, memory = await _get(repo, cpu_id), await _get(repo, memory_id)
        assert cpu.status is memory.status is RecommendationStatus.APPLIED
        assert cpu.verification_window_start == memory.verification_window_start == now
        event = (await repo.get_events(memory_id))[-1]
        assert event.event_type == "rollback_dismissed"
        assert event.payload.model_dump()["reason"] == "restarts caused by a node drain"

        clock = now + timedelta(hours=7)
        updated = await _verifier(repo, _metrics(applied_at, clock), clock).verify_due()

        assert {record.status for record in updated} == {RecommendationStatus.VERIFIED}
        assert _claimed(updated) == pytest.approx(WORKLOAD_DELTA)


class TestFirstMeasurement:
    async def test_one_plan_is_one_change_whatever_the_detection_delay(self, repo):
        """The memory record was detected applied 40 minutes after the CPU record."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=9)
        await _pair(repo, applied_at, memory_applied_at=applied_at + timedelta(minutes=40))

        updated = await _verifier(repo, _metrics(applied_at, now), now).verify_due()

        assert len(updated) == 2
        assert _claimed(updated) == pytest.approx(WORKLOAD_DELTA)

    async def test_unplanned_changes_far_apart_stay_separate(self, repo):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=9)
        await _pair(
            repo,
            applied_at,
            memory_applied_at=applied_at + timedelta(minutes=40),
            baseline={"restart_count": 0},
        )

        assert [len(group) for group in attribution_groups(await _all(repo))] == [1, 1]
