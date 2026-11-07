# tests/core/optimization/test_lifecycle_state_machine.py
"""State-machine and baseline-window regressions (C0.5, C0.11, C0.12; BUG-31, BUG-37, BUG-38)."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import (
    RecommendationConflictError,
    RecommendationLifecycle,
    RecommendationTransitionError,
    freeze_baseline,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import (
    RecommendationEvent,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


def _record(**overrides) -> RecommendationRecord:
    defaults = dict(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=100.0,
        potential_savings_co2e_grams=5000.0,
        evidence=RecommendationEvidence(cost_per_hour_before=0.05, restart_count=0, sample_count=100),
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


async def _saved(repo, **overrides) -> RecommendationRecord:
    await repo.save_recommendations([_record(**overrides)])
    records = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    record = records[0]
    assert record.id is not None
    return record


def _payload(event: RecommendationEvent) -> dict:
    return event.payload.model_dump()


def _id(record: RecommendationRecord) -> int:
    assert record.id is not None
    return record.id


def _baseline(record: RecommendationRecord) -> dict:
    assert record.baseline is not None
    return record.baseline.model_dump()


class TestTransitions:
    async def test_active_cannot_jump_to_verified(self, repo):
        record = await _saved(repo)
        now = datetime.now(timezone.utc)

        with pytest.raises(RecommendationTransitionError):
            await RecommendationLifecycle(repo).mark_verified(
                _id(record),
                measured_co2e_saved_grams=0.0,
                measured_cost_saved=1.0,
                verification_window_start=now,
                verification_window_end=now,
            )

        assert (await repo.get_recommendation_by_id(_id(record))).status is RecommendationStatus.ACTIVE

    @pytest.mark.parametrize(
        "status",
        [
            RecommendationStatus.APPLIED,
            RecommendationStatus.VERIFYING,
            RecommendationStatus.VERIFIED,
            RecommendationStatus.ROLLBACK_REVIEW,
            RecommendationStatus.REVERTED,
            RecommendationStatus.IGNORED,
            RecommendationStatus.EXPIRED,
        ],
    )
    async def test_apply_refuses_records_that_are_not_active_or_pr_open(self, repo, status):
        applied_at = datetime.now(timezone.utc) - timedelta(days=2)
        record = await _saved(repo, status=status, applied_at=applied_at, baseline={"restart_count": 0})

        with pytest.raises(RecommendationTransitionError):
            await RecommendationLifecycle(repo).apply(_id(record), actual_cpu=200)

        unchanged = await repo.get_recommendation_by_id(_id(record))
        assert unchanged.status is status
        assert unchanged.applied_at == applied_at
        assert await repo.get_events(_id(record)) == []

    async def test_apply_from_pr_open_is_allowed(self, repo):
        record = await _saved(repo, status=RecommendationStatus.PR_OPEN)

        updated = await RecommendationLifecycle(repo).apply(_id(record), actual_cpu=300, application_method="pr_merge")

        assert updated.status is RecommendationStatus.APPLIED
        assert [event.event_type for event in await repo.get_events(_id(record))] == ["applied"]

    async def test_concurrent_transition_is_a_conflict_not_an_overwrite(self, repo):
        record = await _saved(repo, status=RecommendationStatus.APPLIED, applied_at=datetime.now(timezone.utc))
        stale = record.model_copy()
        await RecommendationLifecycle(repo).mark_rollback_review(_id(record), reasons=["first writer"])

        class StaleRead:
            """Serves the pre-transition snapshot, like a racing reader would."""

            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            async def get_recommendation_by_id(self, rec_id):
                return stale

        racing = RecommendationLifecycle(StaleRead(repo))  # pyrefly: ignore[bad-argument-type]
        with pytest.raises(RecommendationConflictError):
            await racing.mark_not_realized(_id(record), reasons=["second writer"])

        current = await repo.get_recommendation_by_id(_id(record))
        assert current.status is RecommendationStatus.ROLLBACK_REVIEW
        assert [event.event_type for event in await repo.get_events(_id(record))] == ["rollback_review"]

    async def test_not_realized_and_inconclusive_write_their_own_event_types(self, repo):
        first = await _saved(repo, status=RecommendationStatus.APPLIED, applied_at=datetime.now(timezone.utc))
        lifecycle = RecommendationLifecycle(repo)

        await lifecycle.mark_not_realized(_id(first), reasons=["no saving"])
        await repo.save_recommendations(
            [_record(pod_name="other", status=RecommendationStatus.VERIFYING, applied_at=datetime.now(timezone.utc))]
        )
        second = (await repo.get_recommendations_by_statuses(["verifying"]))[0]
        await lifecycle.mark_inconclusive(_id(second), reason="no trusted samples")

        assert (await repo.get_events(_id(first)))[-1].event_type == "not_realized"
        assert (await repo.get_events(_id(second)))[-1].event_type == "inconclusive"

    async def test_rollback_review_can_be_dismissed_with_a_reason(self, repo):
        applied_at = datetime.now(timezone.utc) - timedelta(days=4)
        record = await _saved(
            repo,
            status=RecommendationStatus.ROLLBACK_REVIEW,
            applied_at=applied_at,
            verification_status="failed",
            baseline={"measurement_ids": ["m1"], "latest_measurement_id": "m1", "measurement_sequence": 1},
        )
        lifecycle = RecommendationLifecycle(repo)

        with pytest.raises(ValueError):
            await lifecycle.dismiss_rollback_review(_id(record), reason="  ")
        updated = await lifecycle.dismiss_rollback_review(_id(record), reason="restart caused by a node drain")

        assert updated.status is RecommendationStatus.APPLIED
        assert updated.verification_status == "pending"
        assert updated.verification_window_start is not None and updated.verification_window_start > applied_at
        baseline = _baseline(updated)
        assert baseline["measurement_sequence"] == 2
        # The counterfactual stays before the change.
        assert datetime.fromisoformat(baseline["baseline_window_end"]) == applied_at
        event = (await repo.get_events(_id(record)))[-1]
        assert event.event_type == "rollback_dismissed"
        assert _payload(event)["reason"] == "restart caused by a node drain"
        assert _payload(event)["dismissed_measurement_id"] == "m1"

    async def test_dismiss_requires_rollback_review(self, repo):
        record = await _saved(repo, status=RecommendationStatus.APPLIED, applied_at=datetime.now(timezone.utc))

        with pytest.raises(RecommendationTransitionError):
            await RecommendationLifecycle(repo).dismiss_rollback_review(_id(record), reason="nope")

    async def test_verified_can_regress(self, repo):
        record = await _saved(repo, status=RecommendationStatus.VERIFIED, applied_at=datetime.now(timezone.utc))

        updated = await RecommendationLifecycle(repo).mark_regressed(_id(record), reasons=["spec drifted"])

        assert updated.status is RecommendationStatus.REGRESSED
        assert (await repo.get_events(_id(record)))[-1].event_type == "regressed"


class TestRemeasure:
    @pytest.mark.parametrize("verification_status", ["failed", "inconclusive"])
    async def test_remeasure_starts_a_new_fixed_window(self, repo, verification_status):
        applied_at = datetime.now(timezone.utc) - timedelta(days=5)
        record = await _saved(
            repo,
            status=RecommendationStatus.APPLIED,
            applied_at=applied_at,
            verification_status=verification_status,
            measured_cost_saved=1.0,
            baseline={
                "measurement_ids": ["m1"],
                "latest_measurement_id": "m1",
                "verification_extended": True,
                "measured_ledger_input": {"sample_count": 12},
            },
        )

        updated = await RecommendationLifecycle(repo).request_remeasure(
            _id(record), reason="load changed", window_hours=24
        )

        assert updated.status is RecommendationStatus.VERIFYING
        assert updated.verification_status == "in_progress"
        assert updated.measured_cost_saved is None
        assert updated.verification_window_start is not None and updated.verification_window_end is not None
        assert updated.verification_window_end - updated.verification_window_start == timedelta(hours=24)
        baseline = _baseline(updated)
        assert baseline["measurement_sequence"] == 2
        assert baseline["verification_extended"] is False
        assert baseline["measured_ledger_input"] == {"sample_count": 12}
        event = (await repo.get_events(_id(record)))[-1]
        assert event.event_type == "remeasure_requested"
        assert _payload(event)["supersedes_measurement_id"] == "m1"
        assert _payload(event)["previous_verification_status"] == verification_status

    @pytest.mark.parametrize(
        ("status", "verification_status"),
        [
            (RecommendationStatus.ACTIVE, None),
            (RecommendationStatus.APPLIED, "pending"),
            (RecommendationStatus.VERIFYING, "in_progress"),
            (RecommendationStatus.VERIFIED, "passed"),
            (RecommendationStatus.ROLLBACK_REVIEW, "failed"),
        ],
    )
    async def test_remeasure_refuses_undecided_or_realized_measurements(self, repo, status, verification_status):
        record = await _saved(
            repo,
            status=status,
            applied_at=datetime.now(timezone.utc) - timedelta(days=5),
            verification_status=verification_status,
        )

        with pytest.raises(RecommendationTransitionError):
            await RecommendationLifecycle(repo).request_remeasure(_id(record))


class TestBaselineWindow:
    def test_captured_at_is_the_evidence_window_end_not_now(self):
        evidence_end = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        record = _record(
            evidence=RecommendationEvidence(
                observation_window_start=evidence_end - timedelta(days=7),
                observation_window_end=evidence_end,
                cost_per_hour_before=0.05,
            )
        )

        baseline = freeze_baseline(record)

        assert baseline["captured_at"] == evidence_end.isoformat()
        assert baseline["evidence_window"]["end"] == evidence_end.isoformat()
        assert datetime.fromisoformat(baseline["frozen_at"]) > evidence_end

    def test_captured_at_falls_back_to_the_recommendation_creation_time(self):
        created_at = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
        record = _record(evidence=None, created_at=created_at)

        baseline = freeze_baseline(record, frozen_at=created_at + timedelta(days=3))

        assert baseline["captured_at"] == created_at.isoformat()
        assert "evidence_window" not in baseline

    async def test_apply_uses_rollout_completion_and_merge_time(self, repo):
        record = await _saved(repo, status=RecommendationStatus.PR_OPEN)
        merged_at = datetime.now(timezone.utc) - timedelta(hours=3)
        for status, moment in (("approved", merged_at - timedelta(hours=5)), ("merged", merged_at)):
            await repo.record_event(
                RecommendationEvent(
                    recommendation_id=_id(record),
                    event_type="plan_status_changed",
                    actor="pr-lifecycle",
                    payload={"details": {"plan_id": "plan-1", "new_status": status}},
                    created_at=moment,
                )
            )
        rollout_done = datetime.now(timezone.utc) - timedelta(hours=1)

        updated = await RecommendationLifecycle(repo).apply(
            _id(record),
            actual_cpu=300,
            application_method="pr_merge",
            actor="detector",
            applied_at=rollout_done,
            plan_id="plan-1",
        )

        assert updated.applied_at == rollout_done
        baseline = _baseline(updated)
        assert datetime.fromisoformat(baseline["baseline_window_end"]) == merged_at
        assert baseline["plan_id"] == "plan-1"
        applied_event = (await repo.get_events(_id(record)))[-1]
        assert applied_event.event_type == "applied"
        assert _payload(applied_event)["applied_at"] == rollout_done.isoformat()

    async def test_manual_apply_falls_back_to_plan_approval(self, repo):
        record = await _saved(repo)
        approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await repo.record_event(
            RecommendationEvent(
                recommendation_id=_id(record),
                event_type="plan_status_changed",
                payload={"details": {"plan_id": "plan-2", "new_status": "approved"}},
                created_at=approved_at,
            )
        )

        updated = await RecommendationLifecycle(repo).apply(_id(record), actual_cpu=300)

        assert datetime.fromisoformat(_baseline(updated)["baseline_window_end"]) == approved_at

    async def test_baseline_never_ends_after_the_change(self, repo):
        record = await _saved(repo)
        applied_at = datetime.now(timezone.utc) - timedelta(hours=6)

        updated = await RecommendationLifecycle(repo).apply(
            _id(record),
            actual_cpu=300,
            applied_at=applied_at,
            baseline_window_end=datetime.now(timezone.utc),
        )

        assert updated.applied_at == applied_at
        assert datetime.fromisoformat(_baseline(updated)["baseline_window_end"]) == applied_at
