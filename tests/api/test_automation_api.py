# tests/api/test_automation_api.py
"""API tests for lifecycle events and the apply-pr automation endpoints."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_pull_request_repository, get_recommendation_repository
from greenkube.automation.source_resolver import (
    ANNOTATION_PATH,
    ANNOTATION_REPO,
    AnnotationSourceResolver,
)
from greenkube.core.db import db_manager
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from tests.automation.test_automation_service import (  # pyrefly: ignore[missing-import]
    MANIFEST,
    FakeProvider,
    FakeReader,
)


@pytest.fixture
async def sqlite_repos():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager), SQLitePullRequestRepository(db_manager)
    await db_manager.close()


def _record(**overrides) -> RecommendationRecord:
    defaults = dict(
        pod_name="payments-api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="payments-api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=300,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        patch={
            "kind": "Deployment",
            "namespace": "prod",
            "name": "payments-api",
            "operations": [{"op": "set_container_resources", "resource": "cpu", "field": "requests", "value": "300m"}],
        },
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


@pytest.fixture
def automation_client(sqlite_repos):
    reco_repo, pr_repo = sqlite_repos
    app = create_app()
    app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
    app.dependency_overrides[get_pull_request_repository] = lambda: pr_repo
    with TestClient(app) as c:
        yield c, reco_repo, pr_repo
    app.dependency_overrides.clear()


class TestEventsEndpoint:
    async def test_returns_404_for_unknown_recommendation(self, automation_client):
        client, _, _ = automation_client
        response = client.get("/api/v1/recommendations/9999/events")
        assert response.status_code == 404

    async def test_apply_records_event_and_baseline(self, automation_client):
        client, reco_repo, _ = automation_client

        await reco_repo.save_recommendations([_record()])
        active = await reco_repo.get_active_recommendations()
        rec_id = active[0].id

        response = client.patch(f"/api/v1/recommendations/{rec_id}/apply", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "applied"
        assert body["application_method"] == "manual"
        assert body["baseline"] is not None

        events = client.get(f"/api/v1/recommendations/{rec_id}/events")
        assert events.status_code == 200
        assert [e["event_type"] for e in events.json()] == ["applied"]


class TestApplyPrEndpoint:
    async def test_dry_run_returns_diff(self, automation_client):
        client, reco_repo, _ = automation_client

        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id

        provider = FakeProvider({"apps/api.yaml": MANIFEST})
        resolver = AnnotationSourceResolver(
            FakeReader({ANNOTATION_REPO: "https://example.test/acme/manifests.git", ANNOTATION_PATH: "apps/api.yaml"})
        )

        from greenkube.automation.service import AutomationService

        def _build(reco, pr):
            return AutomationService(reco_repo=reco, pr_repo=pr, resolver=resolver, provider=provider)

        with patch("greenkube.api.routers.automation._build_service", side_effect=_build):
            response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={"dry_run": True})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "dry_run"
        assert "cpu: 300m" in body["diff"]

    async def test_pull_requests_endpoint(self, automation_client):
        client, reco_repo, _ = automation_client
        response = client.get("/api/v1/recommendations/9999/pull-requests")
        assert response.status_code == 404

    async def test_non_rightsizing_recommendation_returns_422(self, automation_client):
        """Business failures must not be reported as HTTP 200."""
        client, reco_repo, _ = automation_client

        await reco_repo.save_recommendations([_record(type=RecommendationType.ZOMBIE_POD)])
        rec_id = (await reco_repo.get_active_recommendations())[0].id

        response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})

        assert response.status_code == 422
        assert "detail" in response.json()

    async def test_automation_status_endpoint(self, automation_client):
        client, _, _ = automation_client
        response = client.get("/api/v1/automation/status")
        assert response.status_code == 200
        body = response.json()
        assert "token_configured" in body
        assert "provider" in body
