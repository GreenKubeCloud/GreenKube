# tests/api/test_verification_endpoints.py
"""API guards for the verification lifecycle (C0.5, C0.11; BUG-31, BUG-37)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_recommendation_repository
from greenkube.core.db import db_manager
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository


@pytest.fixture
async def reco_repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


@pytest.fixture
def client(reco_repo):
    app = create_app()
    app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _saved(reco_repo, **overrides) -> int:
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
    )
    defaults.update(overrides)
    await reco_repo.save_recommendations([RecommendationRecord(**defaults)])
    records = await reco_repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    assert records[0].id is not None
    return records[0].id


class TestApplyGuard:
    @pytest.mark.parametrize(
        "status",
        [RecommendationStatus.APPLIED, RecommendationStatus.VERIFIED, RecommendationStatus.ROLLBACK_REVIEW],
    )
    async def test_apply_refuses_already_applied_records(self, client, reco_repo, status):
        applied_at = datetime.now(timezone.utc) - timedelta(days=3)
        rec_id = await _saved(reco_repo, status=status, applied_at=applied_at, baseline={"restart_count": 0})

        response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={"actual_cpu_request_millicores": 200})

        assert response.status_code == 409
        record = await reco_repo.get_recommendation_by_id(rec_id)
        assert record.status is status
        assert record.applied_at == applied_at

    async def test_apply_still_works_for_active_records(self, client, reco_repo):
        rec_id = await _saved(reco_repo)

        response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={})

        assert response.status_code == 200
        assert response.json()["status"] == "applied"

    async def test_apply_unknown_recommendation_is_404(self, client):
        assert client.patch("/api/v1/recommendations/9999/apply", json={}).status_code == 404


class TestRemeasure:
    async def test_not_realized_measurement_can_be_remeasured(self, client, reco_repo):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.APPLIED,
            applied_at=datetime.now(timezone.utc) - timedelta(days=4),
            verification_status="failed",
        )

        response = client.post(f"/api/v1/recommendations/{rec_id}/remeasure", json={"reason": "traffic was abnormal"})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "verifying"
        assert body["verification_status"] == "in_progress"
        events = client.get(f"/api/v1/recommendations/{rec_id}/events").json()
        assert events[-1]["event_type"] == "remeasure_requested"

    async def test_body_is_optional(self, client, reco_repo):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.APPLIED,
            applied_at=datetime.now(timezone.utc) - timedelta(days=4),
            verification_status="inconclusive",
        )

        assert client.post(f"/api/v1/recommendations/{rec_id}/remeasure").status_code == 202

    async def test_pending_measurement_cannot_be_remeasured(self, client, reco_repo):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.APPLIED,
            applied_at=datetime.now(timezone.utc),
            verification_status="pending",
        )

        assert client.post(f"/api/v1/recommendations/{rec_id}/remeasure").status_code == 409

    async def test_unknown_recommendation_is_404(self, client):
        assert client.post("/api/v1/recommendations/9999/remeasure").status_code == 404


class TestDismissRollbackReview:
    async def test_dismiss_requires_a_reason(self, client, reco_repo):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.ROLLBACK_REVIEW,
            applied_at=datetime.now(timezone.utc) - timedelta(days=4),
            verification_status="failed",
        )

        assert client.post(f"/api/v1/recommendations/{rec_id}/rollback-review/dismiss", json={}).status_code == 422

        response = client.post(
            f"/api/v1/recommendations/{rec_id}/rollback-review/dismiss",
            json={"reason": "restart came from a node drain"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "applied"
        assert response.json()["verification_status"] == "pending"

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
    async def test_blank_reason_is_422_not_500(self, reco_repo, reason):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.ROLLBACK_REVIEW,
            applied_at=datetime.now(timezone.utc) - timedelta(days=4),
            verification_status="failed",
        )
        app = create_app()
        app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(f"/api/v1/recommendations/{rec_id}/rollback-review/dismiss", json={"reason": reason})

        assert response.status_code == 422
        record = await reco_repo.get_recommendation_by_id(rec_id)
        assert record is not None and record.status is RecommendationStatus.ROLLBACK_REVIEW

    async def test_reason_is_stored_trimmed(self, client, reco_repo):
        rec_id = await _saved(
            reco_repo,
            status=RecommendationStatus.ROLLBACK_REVIEW,
            applied_at=datetime.now(timezone.utc) - timedelta(days=4),
            verification_status="failed",
        )

        response = client.post(
            f"/api/v1/recommendations/{rec_id}/rollback-review/dismiss", json={"reason": "  node drain  "}
        )

        assert response.status_code == 200
        events = await reco_repo.get_events(rec_id)
        assert events[-1].payload.model_dump()["reason"] == "node drain"

    async def test_dismiss_outside_rollback_review_is_409(self, client, reco_repo):
        rec_id = await _saved(reco_repo)

        response = client.post(
            f"/api/v1/recommendations/{rec_id}/rollback-review/dismiss",
            json={"reason": "nothing to dismiss"},
        )

        assert response.status_code == 409
