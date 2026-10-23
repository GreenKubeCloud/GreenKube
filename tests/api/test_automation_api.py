# tests/api/test_automation_api.py
"""API tests for lifecycle events and the apply-pr automation endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_automation_plan_service,
    get_pull_request_repository,
    get_recommendation_repository,
)
from greenkube.automation.plans import AutomationPlanError
from greenkube.core.db import db_manager
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Operation,
    OperationStatus,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationType,
)
from greenkube.storage.sqlite.pull_request_repository import SQLitePullRequestRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

_NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
_DIGEST = "sha256:" + "c" * 64


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


def _plan(recommendation_id: int, status: PlanStatus = PlanStatus.VALIDATED) -> Plan:
    plan_id = "plan-qa-1"
    revision = "a" * 40
    source = WorkloadSource(
        id="source-qa-1",
        cluster="minikube",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="payments-api",
        provider_id="gitea-qa",
        repo_url="http://gitea-qa.example.test/qa/gitops.git",
        ref="main",
        revision=revision,
        path="apps/api.yaml",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="qa-app",
        controller_namespace="argocd",
        source_revision=revision,
        renderer=RendererKind.RAW,
        confidence=0.98,
        status=BindingStatus.RESOLVED,
        observed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
    )
    action = ActionPlanV3(
        action_id=plan_id,
        recommendation_ids=[recommendation_id],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="prod",
            name="payments-api",
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m"}),
            )
        ],
        evidence_ref="recommendation-evidence",
    )
    validation = ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=RendererKind.RAW,
        output_digest="sha256:" + "d" * 64,
    )
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(
                path="apps/api.yaml",
                operation="update",
                original_sha="b" * 40,
                content="apiVersion: apps/v1\nkind: Deployment\n",
            )
        ],
        diff="diff --git a/apps/api.yaml\n+            cpu: 300m",
        rendered_diff="diff --git a/rendered/api.yaml\n+            cpu: 300m",
        base_revision=revision,
        source_revision=revision,
        renderer=RendererKind.RAW,
        validation=validation,
        digest=_DIGEST,
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=[recommendation_id],
        source=source,
        action=action,
        file_plan=file_plan,
        status=status,
        digest=_DIGEST,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
    )


class _FakePlanApiService:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.operation: Operation | None = None
        self.prepare_calls: list[list[int]] = []
        self.preview_calls: list[list[int]] = []
        self.approve_calls: list[tuple[str, str]] = []

    async def prepare_plan(self, recommendation_ids: list[int], *, actor: str = "system") -> Plan:
        self.prepare_calls.append(recommendation_ids)
        return self.plan

    async def preview_plan(self, recommendation_ids: list[int]) -> Plan:
        self.preview_calls.append(recommendation_ids)
        return self.plan

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan:
        self.approve_calls.append((plan_id, digest))
        self.plan = self.plan.model_copy(update={"status": PlanStatus.APPROVED})
        self.operation = Operation(
            operation_id="op-qa-1",
            plan_id=plan_id,
            idempotency_key=f"plan:{plan_id}:{digest}",
        )
        return self.plan

    async def read_plan(self, plan_id: str) -> Plan | None:
        return self.plan if self.plan.plan_id == plan_id else None

    async def read_operation_for_plan(self, plan_id: str) -> Operation | None:
        return self.operation if self.operation and self.operation.plan_id == plan_id else None

    async def read_operation(self, operation_id: str) -> Operation | None:
        return self.operation if self.operation and self.operation.operation_id == operation_id else None

    async def prepare_rollback(self, plan_id: str) -> Plan:
        raise NotImplementedError

    async def mark_stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        if self.plan.plan_id != plan_id:
            return None
        self.plan = self.plan.model_copy(update={"status": PlanStatus.STALE})
        return self.plan


@pytest.fixture
def automation_client(sqlite_repos):
    reco_repo, pr_repo = sqlite_repos
    app = create_app()
    app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
    app.dependency_overrides[get_pull_request_repository] = lambda: pr_repo
    with TestClient(app) as c:
        yield c, reco_repo, pr_repo, app
    app.dependency_overrides.clear()


class TestEventsEndpoint:
    async def test_returns_404_for_unknown_recommendation(self, automation_client):
        client, _, _, _ = automation_client
        response = client.get("/api/v1/recommendations/9999/events")
        assert response.status_code == 404

    async def test_apply_records_event_and_baseline(self, automation_client):
        client, reco_repo, _, _ = automation_client

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
    async def test_dry_run_uses_resolved_plan_without_workload_annotations(self, automation_client):
        client, reco_repo, _, app = automation_client

        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        service = _FakePlanApiService(_plan(rec_id))
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={"dry_run": True})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "dry_run"
        assert body["repo"] == "http://gitea-qa.example.test/qa/gitops.git"
        assert body["path"] == "apps/api.yaml"
        assert body["policy_result"] == {}
        assert "cpu: 300m" in body["diff"]
        assert service.preview_calls == [[rec_id]]
        assert service.prepare_calls == []

    async def test_dry_run_surfaces_typed_policy_rejection(self, automation_client):
        client, reco_repo, _, app = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        policy_result = {
            "version": 1,
            "allowed": False,
            "checks": [{"gate": "cooldown", "state": "rejected"}],
            "rejections": [
                {
                    "code": "workload_cooldown_active",
                    "detail": "Retry at the recorded cooldown deadline.",
                }
            ],
        }

        class PolicyRejectedService(_FakePlanApiService):
            async def preview_plan(self, recommendation_ids: list[int]) -> Plan:
                raise AutomationPlanError(
                    AutomationErrorCode.WORKLOAD_COOLDOWN_ACTIVE,
                    "internal cooldown details",
                    policy_result=policy_result,
                )

        service = PolicyRejectedService(_plan(rec_id))
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={"dry_run": True})

        assert response.status_code == 429
        assert response.json()["code"] == "workload_cooldown_active"
        assert response.json()["policy_result"] == policy_result
        assert "internal cooldown details" not in response.text
        assert service.approve_calls == []

    async def test_eligibility_check_previews_without_persisting_plan(self, automation_client):
        client, reco_repo, _, app = automation_client

        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        service = _FakePlanApiService(_plan(rec_id))
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        response = client.get(f"/api/v1/recommendations/{rec_id}/apply-pr/eligibility")

        assert response.status_code == 200
        assert response.json()["eligible"] is True
        assert service.preview_calls == [[rec_id]]
        assert service.prepare_calls == []

    async def test_pull_requests_endpoint(self, automation_client):
        client, reco_repo, _, _ = automation_client
        response = client.get("/api/v1/recommendations/9999/pull-requests")
        assert response.status_code == 404

    async def test_non_dry_run_approves_plan_and_returns_its_operation(self, automation_client):
        client, reco_repo, _, app = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        service = _FakePlanApiService(_plan(rec_id))
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        response = client.post(
            f"/api/v1/recommendations/{rec_id}/apply-pr",
            json={},
            headers={"Idempotency-Key": "api-request-1", "X-Audit-Actor": "release-bot"},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["operation_id"] == "op-qa-1"
        assert body["plan_id"] == "plan-qa-1"
        assert body["idempotency_key"] == f"plan:plan-qa-1:{_DIGEST}"
        assert body["actor"] == "anonymous"
        assert service.approve_calls == [("plan-qa-1", _DIGEST)]

    async def test_repeated_non_dry_run_reuses_the_plan_operation(self, automation_client):
        client, reco_repo, _, app = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        service = _FakePlanApiService(_plan(rec_id))
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        first = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})
        second = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})

        assert first.status_code == second.status_code == 202
        assert first.json()["operation_id"] == second.json()["operation_id"] == "op-qa-1"
        assert service.approve_calls == [("plan-qa-1", _DIGEST)]

    async def test_repeated_apply_pr_reuses_an_existing_open_recommendation_pr(self, automation_client):
        client, reco_repo, pr_repo, app = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        plan = _plan(rec_id)
        service = _FakePlanApiService(plan)
        await pr_repo.save_pull_request(
            PullRequestRecord(
                recommendation_id=rec_id,
                provider="gitea",
                provider_id="gitea-review",
                repo="greenkube-qa/k8s-greenkube-review-20261022",
                base_branch="main",
                head_branch="greenkube/plan-test",
                pr_number=7,
                pr_url="http://gitea.local/greenkube-qa/k8s-greenkube-review-20261022/pulls/7",
                status=PullRequestStatus.OPEN,
                plan_id=plan.plan_id,
            )
        )
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        first = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})
        second = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})

        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == second.json()["status"] == "pr_open"
        assert first.json()["pull_request"]["id"] == second.json()["pull_request"]["id"]
        assert service.prepare_calls == []
        assert len(await pr_repo.get_open_pull_requests(rec_id)) == 1

    async def test_operation_status_reads_the_plan_queue(self, automation_client):
        client, reco_repo, _, app = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id
        service = _FakePlanApiService(_plan(rec_id, status=PlanStatus.APPROVED))
        service.operation = Operation(
            operation_id="op-qa-1",
            plan_id="plan-qa-1",
            idempotency_key=f"plan:plan-qa-1:{_DIGEST}",
            status=OperationStatus.RUNNING,
            attempts=2,
        )
        app.dependency_overrides[get_automation_plan_service] = lambda: service

        response = client.get("/api/v1/automation/operations/op-qa-1")

        assert response.status_code == 200
        assert response.json()["status"] == "running"
        assert response.json()["attempts"] == 2
        assert response.json()["plan_id"] == "plan-qa-1"

    async def test_apply_pr_reports_missing_plan_service(self, automation_client):
        client, reco_repo, _, _ = automation_client
        await reco_repo.save_recommendations([_record()])
        rec_id = (await reco_repo.get_active_recommendations())[0].id

        response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={"dry_run": True})

        assert response.status_code == 503
        assert response.json()["code"] == "plan_service_unavailable"

    async def test_non_rightsizing_recommendation_returns_422(self, automation_client):
        """Business failures must not be reported as HTTP 200."""
        client, reco_repo, _, app = automation_client

        await reco_repo.save_recommendations([_record(type=RecommendationType.ZOMBIE_POD)])
        rec_id = (await reco_repo.get_active_recommendations())[0].id

        class NonRightsizingPlanService(_FakePlanApiService):
            async def prepare_plan(self, recommendation_ids: list[int], *, actor: str = "system") -> Plan:
                raise AutomationPlanError(
                    AutomationErrorCode.REQUIRES_CONTAINER_TARGET,
                    "Only actionable resource recommendations can be planned.",
                )

        app.dependency_overrides[get_automation_plan_service] = lambda: NonRightsizingPlanService(_plan(rec_id))
        response = client.post(f"/api/v1/recommendations/{rec_id}/apply-pr", json={})

        assert response.status_code == 422
        assert "detail" in response.json()

    async def test_automation_status_endpoint(self, automation_client):
        client, _, _, _ = automation_client
        response = client.get("/api/v1/automation/status")
        assert response.status_code == 200
        body = response.json()
        assert "token_configured" in body
        assert "provider" in body
        assert body["mode"] in {"disabled", "inline", "worker"}
        assert "ready" in body
        assert "worker" in body
        assert "queue" in body
        assert "discovery" in body
        assert "renderer" in body
        assert isinstance(body["providers"], list)
