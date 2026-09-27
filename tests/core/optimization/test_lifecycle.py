# tests/core/optimization/test_lifecycle.py
"""Tests for the recommendation lifecycle service and its audit trail."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.db import db_manager
from greenkube.core.optimization.lifecycle import RecommendationLifecycle, freeze_baseline
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import (
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
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        potential_savings_cost=100.0,
        potential_savings_co2e_grams=5000.0,
        evidence=RecommendationEvidence(
            cost_per_hour_before=0.05,
            co2e_grams_per_hour_before=2.5,
            restart_count=0,
            sample_count=100,
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


class TestFreezeBaseline:
    def test_captures_evidence_fields(self):
        baseline = freeze_baseline(_record())
        assert baseline["cost_per_hour_before"] == 0.05
        assert baseline["co2e_grams_per_hour_before"] == 2.5
        assert baseline["current_cpu_request_millicores"] == 1000
        assert baseline["restart_count"] == 0

    def test_works_without_evidence(self):
        baseline = freeze_baseline(_record(evidence=None))
        assert baseline["current_cpu_request_millicores"] == 1000
        assert "cost_per_hour_before" not in baseline


class TestLifecycleApply:
    @pytest.mark.asyncio
    async def test_apply_sets_baseline_and_event(self, repo):
        await repo.save_recommendations([_record()])
        active = await repo.get_active_recommendations()
        rec_id = active[0].id

        lifecycle = RecommendationLifecycle(repo)
        updated = await lifecycle.apply(rec_id, actual_cpu=350, application_method="manual")

        assert updated.status == RecommendationStatus.APPLIED
        assert updated.application_method == "manual"
        assert updated.applied_at is not None
        assert updated.verification_status == "pending"
        assert updated.baseline is not None
        assert updated.baseline["cost_per_hour_before"] == 0.05

        events = await repo.get_events(rec_id)
        assert [e.event_type for e in events] == ["applied"]
        assert events[0].actor == "user"

    @pytest.mark.asyncio
    async def test_apply_unknown_id_raises(self, repo):
        lifecycle = RecommendationLifecycle(repo)
        with pytest.raises(ValueError):
            await lifecycle.apply(9999)


class TestLifecycleExpire:
    @pytest.mark.asyncio
    async def test_expire_marks_ttl_elapsed_records(self, repo):
        await repo.save_recommendations(
            [
                _record(pod_name="old", expires_at=datetime.now(timezone.utc) - timedelta(hours=1)),
                _record(pod_name="fresh", expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
            ]
        )

        lifecycle = RecommendationLifecycle(repo)
        expired = await lifecycle.expire()

        assert len(expired) == 1
        assert expired[0].pod_name == "old"
        active = await repo.get_active_recommendations()
        assert [r.pod_name for r in active] == ["fresh"]

        events = await repo.get_events(expired[0].id)
        assert [e.event_type for e in events] == ["expired"]

    @pytest.mark.asyncio
    async def test_reconcile_does_not_touch_expired(self, repo):
        await repo.save_recommendations([_record(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))])
        lifecycle = RecommendationLifecycle(repo)
        await lifecycle.expire()

        # reconcile with an empty generated set must not mark expired as stale
        await repo.reconcile_active_recommendations([])
        expired = await repo.get_recommendations_by_statuses(["expired"])
        assert len(expired) == 1


class TestLifecycleVerification:
    @pytest.mark.asyncio
    async def test_mark_verified_sets_measured_values(self, repo):
        await repo.save_recommendations([_record()])
        rec_id = (await repo.get_active_recommendations())[0].id
        lifecycle = RecommendationLifecycle(repo)
        now = datetime.now(timezone.utc)

        updated = await lifecycle.mark_verified(
            rec_id,
            measured_co2e_saved_grams=4000.0,
            measured_cost_saved=80.0,
            verification_window_start=now - timedelta(hours=1),
            verification_window_end=now,
        )

        assert updated.status == RecommendationStatus.VERIFIED
        assert updated.verified_at is not None
        assert updated.verification_status == "passed"
        assert updated.savings_realized is True
        assert updated.measured_cost_saved == 80.0

        events = await repo.get_events(rec_id)
        assert "verified" in [e.event_type for e in events]
