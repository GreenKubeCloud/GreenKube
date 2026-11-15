"""Contract tests for digest-addressed automation plan endpoints."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_automation_plan_service
from greenkube.automation.plans import AutomationPlanError, AutomationPlanService
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    FilePlanSummary,
    Operation,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.storage.base_gitops_repository import AutomationPlanRecord, AutomationPlanRepository

_NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
_BASE_REVISION = "a" * 40
_DIGEST = "sha256:" + "c" * 64
_SECRET = "plan-api-test-secret"


def _plan(
    *,
    recommendation_ids: Sequence[int] = (42,),
    status: PlanStatus = PlanStatus.VALIDATED,
    plan_id: str = "plan-1",
    rollback_of_plan_id: str | None = None,
    renderer: RendererKind = RendererKind.RAW,
) -> Plan:
    ids = list(recommendation_ids)
    flux_helm_release = renderer is RendererKind.HELM_RELEASE
    source = WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="payments-api",
        provider_id="github-main",
        repo_url=f"https://user:{_SECRET}@github.example.test/acme/gitops.git?access_token={_SECRET}",
        ref="main",
        revision=_BASE_REVISION,
        path="apps/payments",
        controller_kind=ControllerKind.FLUX_HELM_RELEASE if flux_helm_release else ControllerKind.ARGOCD,
        controller_name="payments" if flux_helm_release else "payments-prod",
        controller_namespace="flux-system" if flux_helm_release else "argocd",
        source_revision=_BASE_REVISION,
        renderer=renderer,
        renderer_config={"password": _SECRET},
        confidence=0.98,
        status=BindingStatus.RESOLVED,
        evidence={"credential": _SECRET},
        observed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
    )
    action = ActionPlanV3(
        action_id=plan_id,
        recommendation_ids=ids,
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="payments-api",
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"}),
            )
        ],
        evidence_ref=_SECRET,
    )
    validation = ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=renderer,
        output_digest="sha256:" + "d" * 64,
        warnings=[_SECRET],
        reason=_SECRET,
    )
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(
                path="apps/payments/helmrelease.yaml" if flux_helm_release else "apps/payments/deployment.yaml",
                operation="update",
                original_sha="b" * 40,
                content="updated HelmRelease" if flux_helm_release else "updated manifest",
            )
        ],
        diff="--- a/apps/payments/deployment.yaml\n+++ b/apps/payments/deployment.yaml\n",
        rendered_diff="--- rendered before\n+++ rendered after\n",
        base_revision=_BASE_REVISION,
        source_revision=_BASE_REVISION,
        renderer=renderer,
        validation=validation,
        warnings=[
            "apps/payments/deployment.yaml#0: ignored volatile metadata key 'resourceVersion'",
            f"https://user:{_SECRET}@github.example.test/acme/gitops.git?access_token={_SECRET} is empty",
            f"helm warning: token={_SECRET}; ghp_{_SECRET.replace('-', '')} leaked\x1b[0m",
        ],
        digest=_DIGEST,
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=ids,
        source=source,
        action=action,
        file_plan=file_plan,
        status=status,
        digest=_DIGEST,
        rollback_of_plan_id=rollback_of_plan_id,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
    )


class _MemoryPlanRepository(AutomationPlanRepository):
    def __init__(self, plans: Sequence[Plan] = ()) -> None:
        self.plans = {plan.plan_id: plan for plan in plans}
        self.action_status: dict[int, ActionStatus] = {}

    async def save_plan(
        self,
        plan: Plan,
        *,
        policy_result: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Plan:
        self.plans[plan.plan_id] = plan
        return plan

    async def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None:
        plan = await self.get_plan(plan_id)
        return AutomationPlanRecord(plan) if plan is not None else None

    async def list_plans(
        self,
        *,
        status: PlanStatus | None = None,
        source_id: str | None = None,
    ) -> list[Plan]:
        return [
            plan
            for plan in self.plans.values()
            if (status is None or plan.status is status) and (source_id is None or plan.source.id == source_id)
        ]

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        updated = plan.model_copy(update={"status": status})
        self.plans[plan_id] = updated
        return updated

    async def list_plans_for_recommendation(self, recommendation_id: int) -> list[Plan]:
        return [plan for plan in self.plans.values() if recommendation_id in plan.recommendation_ids]

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        for recommendation_id in recommendation_ids:
            self.action_status[recommendation_id] = status
        return len(set(recommendation_ids))

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        return self.action_status.get(recommendation_id)


class _FakeRevisionResolver:
    async def resolve_current_revision(self, source: WorkloadSource) -> str:
        return _BASE_REVISION


class _IdempotentPlanOperationQueue:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.operations: dict[str, str] = {}

    async def enqueue(self, plan: Plan, *, idempotency_key: str) -> object:
        self.keys.append(idempotency_key)
        self.operations.setdefault(idempotency_key, plan.plan_id)
        return self.operations[idempotency_key]


class _FakePreparationService:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.calls: list[list[int]] = []
        self.mappings: list[dict[str, Any] | None] = []

    async def prepare_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        actor: str = "system",
        values_mapping: dict[str, Any] | None = None,
    ) -> Plan:
        self.calls.append(list(recommendation_ids))
        self.mappings.append(values_mapping)
        return self.plan


class _FailingPreparationService:
    def __init__(self, error: AutomationPlanError) -> None:
        self.error = error

    async def prepare_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        actor: str = "system",
        values_mapping: dict[str, Any] | None = None,
    ) -> Plan:
        raise self.error


class _AutomationPlanApiService:
    def __init__(self, plans: AutomationPlanService, preparation: Any) -> None:
        self.plans = plans
        self.preparation = preparation
        self.rollback_requests: list[str] = []
        self.rollback_plan: Plan | None = None
        self.rollback_error: AutomationPlanError | None = None

    async def prepare_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        actor: str = "system",
        values_mapping: dict[str, Any] | None = None,
    ) -> Plan:
        return await self.preparation.prepare_plan(
            recommendation_ids,
            actor=actor,
            values_mapping=values_mapping,
        )

    async def preview_plan(self, recommendation_ids: Sequence[int]) -> Plan:
        return await self.preparation.prepare_plan(recommendation_ids)

    async def read_plan(self, plan_id: str) -> Plan | None:
        return await self.plans.read_plan(plan_id)

    async def read_operation_for_plan(self, plan_id: str) -> Operation | None:
        plan = await self.plans.read_plan(plan_id)
        if plan is None or plan.status is not PlanStatus.APPROVED:
            return None
        assert plan.digest is not None
        return Operation(
            operation_id=f"operation-{plan_id}",
            plan_id=plan_id,
            idempotency_key=f"plan:{plan_id}:{plan.digest}",
        )

    async def prepare_rollback(self, plan_id: str) -> Plan:
        self.rollback_requests.append(plan_id)
        if self.rollback_error is not None:
            raise self.rollback_error
        if self.rollback_plan is None:
            raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, "no rollback plan fixture")
        return self.rollback_plan

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan:
        return await self.plans.approve_plan(plan_id, digest, actor=actor)

    async def mark_stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None:
        return await self.plans.mark_stale(plan_id, actor=actor, reason_code=reason_code)

    async def cancel_plan(self, plan_id: str, *, actor: str = "system") -> Plan | None:
        return await self.plans.cancel_plan(plan_id, actor=actor)


@pytest.fixture
def plan_api():
    app = create_app()
    repository = _MemoryPlanRepository([_plan()])
    queue = _IdempotentPlanOperationQueue()
    service = AutomationPlanService(
        repository,
        revision_resolver=_FakeRevisionResolver(),
        operation_enqueuer=queue,
        clock=lambda: _NOW,
    )
    api_service = _AutomationPlanApiService(service, _FakePreparationService(_plan()))
    app.dependency_overrides[get_automation_plan_service] = lambda: api_service
    with TestClient(app) as client:
        yield client, app, repository, api_service, queue
    app.dependency_overrides.clear()


def test_create_uses_only_recommendation_ids_and_returns_safe_plan_projection(plan_api):
    client, _, _, service, _ = plan_api
    policy_result = {
        "version": 1,
        "allowed": True,
        "checks": [{"gate": "minimum_confidence", "state": "passed"}],
        "rejections": [],
    }
    preparation = _FakePreparationService(
        _plan(recommendation_ids=(42, 43)).model_copy(update={"policy_result": policy_result})
    )
    service.preparation = preparation

    response = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": [42, 43]},
        headers={"X-Audit-Actor": _SECRET},
    )

    assert response.status_code == 201
    assert preparation.calls == [[42, 43]]
    body = response.json()
    assert body["plan_id"] == "plan-1"
    assert body["recommendation_ids"] == [42, 43]
    assert body["status"] == "validated"
    assert body["digest"] == _DIGEST
    assert body["policy_result"] == policy_result
    assert body["files"] == [
        {
            "path": "apps/payments/deployment.yaml",
            "operation": "update",
            "original_sha": "b" * 40,
        }
    ]
    # D12 / BUG-56: real renderer warnings, with credentials redacted.
    assert body["warnings"] == [
        "apps/payments/deployment.yaml#0: ignored volatile metadata key 'resourceVersion'",
        "https://[redacted]@github.example.test/acme/gitops.git?[redacted] is empty",
        "helm warning: token=[redacted]; [redacted] leaked [0m",
    ]
    assert body["warnings_truncated"] is False
    assert body["source"]["repo"] == "https://github.example.test/acme/gitops.git"
    assert "actor" not in body
    assert _SECRET not in response.text
    assert "evidence" not in body
    assert "action" not in body
    assert "renderer_config" not in response.text
    assert "content" not in body["files"][0]


def test_create_forwards_only_the_one_time_values_mapping(plan_api):
    client, _, _, service, _ = plan_api
    preparation = _FakePreparationService(_plan())
    service.preparation = preparation
    mapping = {
        "file_path": "apps/payments/values.yaml",
        "container": "api",
        "path": ["app", "resources"],
    }

    response = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": [42], "values_mapping": mapping},
    )

    assert response.status_code == 201
    assert preparation.calls == [[42]]
    assert preparation.mappings == [mapping]


def test_create_returns_explicit_503_without_server_side_preparation(plan_api):
    client, app, _, _, _ = plan_api
    app.dependency_overrides.pop(get_automation_plan_service)

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 503
    assert response.json() == {
        "code": "plan_preparation_unavailable",
        "detail": "Server-side plan preparation is not configured.",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"action": {"action_id": "forged"}},
        {"source": {"repo_url": "https://attacker.invalid/repo"}},
        {"repo_url": "https://attacker.invalid/repo"},
        {"path": "attacker/path"},
        {"provider_id": "attacker"},
        {"binding": {"source_id": "attacker"}},
        {
            "values_mapping": {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": ["app", "resources"],
                "repo_url": "https://attacker.invalid/repo",
            }
        },
        {
            "values_mapping": {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": ["app", "resources"],
                "source_index": 1,
            }
        },
        {
            "values_mapping": {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": ["app", "resources"],
                "credentials": "attacker-token",
            }
        },
        {
            "values_mapping": {
                "file_path": "apps/payments/values.yaml",
                "container": "api",
                "path": [],
            }
        },
    ],
)
def test_create_rejects_client_supplied_plan_authority(plan_api, override):
    client, _, _, service, _ = plan_api
    preparation = _FakePreparationService(_plan())
    service.preparation = preparation

    response = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": [42], **override},
    )

    assert response.status_code == 422
    assert preparation.calls == []


def test_create_enforces_recommendation_id_count_and_values(plan_api):
    client, _, _, service, _ = plan_api
    preparation = _FakePreparationService(_plan())
    service.preparation = preparation

    too_many = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": list(range(1, 102))},
    )
    duplicate = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": [42, 42]},
    )
    non_positive = client.post(
        "/api/v1/automation/plans",
        json={"recommendation_ids": [0]},
    )

    assert too_many.status_code == 422
    assert duplicate.status_code == 422
    assert non_positive.status_code == 422
    assert preparation.calls == []


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        (AutomationErrorCode.PROVIDER_NOT_READY, 424),
        (AutomationErrorCode.PROVIDER_CAPABILITY_MISSING, 424),
        (AutomationErrorCode.POLICY_EVIDENCE_MISSING, 424),
    ],
)
def test_create_maps_provider_and_policy_errors_without_leaking_internal_messages(plan_api, code, status_code):
    client, _, _, service, _ = plan_api
    service.preparation = _FailingPreparationService(AutomationPlanError(code, _SECRET))

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == status_code
    assert response.json()["code"] == code.value
    assert "reason" not in response.json()
    assert _SECRET not in response.text


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        (AutomationErrorCode.PLAN_STALE, 409),
        (AutomationErrorCode.SOURCE_CHANGED, 409),
        (AutomationErrorCode.UNSUPPORTED_SOURCE, 422),
        (AutomationErrorCode.AMBIGUOUS_BINDING, 422),
        (AutomationErrorCode.REQUIRES_CONTAINER_TARGET, 422),
        (AutomationErrorCode.RENDER_VALIDATION_FAILED, 422),
    ],
)
def test_create_returns_the_planner_reason_with_the_stable_detail(plan_api, code, status_code):
    """The planner's reason is returned so a failed preview is explained without the pod logs."""
    client, _, _, service, _ = plan_api
    service.preparation = _FailingPreparationService(
        AutomationPlanError(code, "recommendations do not share one persisted action")
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code.value
    assert body["detail"]
    assert body["reason"] == "recommendations do not share one persisted action"


def test_create_redacts_credentials_in_the_planner_reason(plan_api):
    client, _, _, service, _ = plan_api
    message = (
        "render failed for https://user:hunter2@git.example/repo.git?private_token=abc123 "
        "with token=glpat_supersecretvalue\nsecond line " + "x" * 400
    )
    service.preparation = _FailingPreparationService(
        AutomationPlanError(AutomationErrorCode.RENDER_VALIDATION_FAILED, message)
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    reason = response.json()["reason"]
    assert "hunter2" not in response.text
    assert "abc123" not in response.text
    assert "supersecretvalue" not in response.text
    assert "\n" not in reason
    assert reason.startswith("render failed for https://[redacted]@git.example/repo.git?[redacted]")
    assert len(reason) <= 300


def test_create_returns_typed_policy_rejection_with_structured_result(plan_api):
    client, _, _, service, _ = plan_api
    policy_result = {
        "version": 1,
        "allowed": False,
        "checks": [
            {
                "gate": "open_pull_request_budget",
                "state": "rejected",
                "observed": 1,
                "limit": 1,
            }
        ],
        "rejections": [
            {
                "code": "workload_pr_limit_exceeded",
                "detail": "Close or merge the existing pull request before retrying.",
            }
        ],
    }
    service.preparation = _FailingPreparationService(
        AutomationPlanError(
            AutomationErrorCode.WORKLOAD_PR_LIMIT_EXCEEDED,
            "internal policy detail",
            policy_result=policy_result,
        )
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 429
    assert response.json()["code"] == "workload_pr_limit_exceeded"
    assert response.json()["policy_result"] == policy_result
    assert "internal policy detail" not in response.text


def test_create_returns_only_safe_mapping_choices_when_mapping_is_needed(plan_api):
    client, _, _, service, _ = plan_api
    service.preparation = _FailingPreparationService(
        AutomationPlanError(
            AutomationErrorCode.NEEDS_CHART_MAPPING,
            "internal mapping detail",
            mapping_options={
                "files": ["apps/payments/values.yaml"],
                "containers": ["api"],
            },
        )
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 422
    assert response.json()["mapping_options"] == {
        "files": ["apps/payments/values.yaml"],
        "containers": ["api"],
    }
    assert "internal mapping detail" not in response.text
    assert "repo_url" not in response.text
    assert "credentials" not in response.text


def test_get_plan_returns_safe_projection_and_404_for_missing_plan(plan_api):
    client, _, _, _, _ = plan_api

    found = client.get("/api/v1/automation/plans/plan-1")
    missing = client.get("/api/v1/automation/plans/missing")

    assert found.status_code == 200
    assert _SECRET not in found.text
    assert "evidence_ref" not in found.text
    assert "renderer_config" not in found.text
    assert missing.status_code == 404
    assert missing.json()["code"] == "plan_not_found"


def test_get_plan_projects_a_plan_whose_files_were_pruned(plan_api):
    """D2 / BUG-44: a pruned APPLIED plan must stay readable (previously HTTP 500)."""
    client, _, repository, _, _ = plan_api
    live = _plan(status=PlanStatus.APPLIED)
    assert live.file_plan is not None
    summary = FilePlanSummary.from_file_plan(live.file_plan, pruned_at=_NOW)
    repository.plans["plan-1"] = live.model_copy(update={"file_plan": None, "file_plan_summary": summary})

    response = client.get("/api/v1/automation/plans/plan-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert body["source"]["base_revision"] == _BASE_REVISION
    assert body["files"] == [{"path": "apps/payments/deployment.yaml", "operation": "update", "original_sha": "b" * 40}]
    assert body["validation"]["passed"] is True
    assert body["diff"] is None
    assert _SECRET not in response.text


def test_rollback_preview_returns_a_persisted_inverse_plan_link(plan_api):
    client, _, _, service, _ = plan_api
    service.rollback_plan = _plan(
        plan_id="rbk-test-plan",
        rollback_of_plan_id="plan-1",
    )

    response = client.post("/api/v1/automation/plans/plan-1/rollback-preview")

    assert response.status_code == 201
    assert service.rollback_requests == ["plan-1"]
    assert response.json()["plan_id"] == "rbk-test-plan"
    assert response.json()["rollback_of_plan_id"] == "plan-1"
    assert response.json()["status"] == "validated"


def test_rollback_preview_projects_a_flux_helmrelease_inverse_plan(plan_api):
    client, _, _, service, _ = plan_api
    service.rollback_plan = _plan(
        plan_id="rbk-flux-plan",
        rollback_of_plan_id="plan-1",
        renderer=RendererKind.HELM_RELEASE,
    )

    response = client.post("/api/v1/automation/plans/plan-1/rollback-preview")

    assert response.status_code == 201
    assert response.json()["rollback_of_plan_id"] == "plan-1"
    assert response.json()["source"]["renderer"] == RendererKind.HELM_RELEASE.value
    assert response.json()["validation"]["renderer"] == RendererKind.HELM_RELEASE.value
    assert response.json()["files"] == [
        {"path": "apps/payments/helmrelease.yaml", "operation": "update", "original_sha": "b" * 40}
    ]
    assert _SECRET not in response.text
    assert "renderer_config" not in response.text


def test_rollback_preview_returns_stable_unsupported_reason(plan_api):
    client, _, _, service, _ = plan_api
    service.rollback_error = AutomationPlanError(
        AutomationErrorCode.ROLLBACK_UNSUPPORTED,
        "internal renderer detail",
    )

    response = client.post("/api/v1/automation/plans/plan-1/rollback-preview")

    assert response.status_code == 422
    assert response.json() == {
        "code": "rollback_unsupported",
        "detail": "This plan cannot be reversed safely.",
        "reason": "internal renderer detail",
    }


def test_rollback_preview_returns_not_ready_when_the_original_baseline_is_missing(plan_api):
    client, _, _, service, _ = plan_api
    service.rollback_error = AutomationPlanError(
        AutomationErrorCode.ROLLBACK_NOT_READY,
        "the persisted pre-apply baseline is missing",
    )

    response = client.post("/api/v1/automation/plans/plan-1/rollback-preview")

    assert response.status_code == 409
    assert response.json() == {
        "code": "rollback_not_ready",
        "detail": "The linked recommendations are not ready for rollback.",
        "reason": "the persisted pre-apply baseline is missing",
    }


def test_approve_requires_exact_digest_and_does_not_enqueue_on_mismatch(plan_api):
    client, _, _, _, queue = plan_api

    response = client.post(
        "/api/v1/automation/plans/plan-1/approve",
        json={"digest": "sha256:" + "f" * 64},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "plan_stale"
    assert queue.keys == []


def test_approve_enqueues_idempotently_by_plan_and_digest(plan_api):
    client, _, _, _, queue = plan_api

    first = client.post("/api/v1/automation/plans/plan-1/approve", json={"digest": _DIGEST})
    second = client.post("/api/v1/automation/plans/plan-1/approve", json={"digest": _DIGEST})

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "approved"
    assert second.json()["status"] == "approved"
    assert first.json()["operation_id"] == "operation-plan-1"
    assert second.json()["operation_status"] == "queued"
    assert queue.keys == [f"plan:plan-1:{_DIGEST}", f"plan:plan-1:{_DIGEST}"]
    assert queue.operations == {f"plan:plan-1:{_DIGEST}": "plan-1"}


def test_approve_rejects_extra_request_overrides(plan_api):
    client, _, _, _, queue = plan_api

    response = client.post(
        "/api/v1/automation/plans/plan-1/approve",
        json={"digest": _DIGEST, "repo_url": "https://attacker.invalid/repo"},
    )

    assert response.status_code == 422
    assert queue.keys == []


def test_cancel_marks_non_applying_plan_stale_and_conflicts_for_applying_plan(plan_api):
    client, _, repository, _, _ = plan_api

    cancelled = client.post("/api/v1/automation/plans/plan-1/cancel")
    repository.plans["applying-plan"] = _plan(status=PlanStatus.APPLYING).model_copy(
        update={"plan_id": "applying-plan"}
    )
    applying = client.post("/api/v1/automation/plans/applying-plan/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "stale"
    assert applying.status_code == 409
    assert applying.json()["code"] == "plan_stale"


def test_delete_cancels_a_preview_and_conflicts_for_progressed_plans(plan_api):
    """D6 / BUG-48: dismissing a preview cancels it so re-opening never returns 409."""
    client, _, repository, _, _ = plan_api
    repository.plans["applying-plan"] = _plan(status=PlanStatus.APPLYING).model_copy(
        update={"plan_id": "applying-plan"}
    )
    repository.plans["approved-plan"] = _plan(status=PlanStatus.APPROVED).model_copy(
        update={"plan_id": "approved-plan"}
    )

    deleted = client.delete("/api/v1/automation/plans/plan-1")
    deleted_again = client.delete("/api/v1/automation/plans/plan-1")
    missing = client.delete("/api/v1/automation/plans/missing")
    applying = client.delete("/api/v1/automation/plans/applying-plan")
    approved = client.delete("/api/v1/automation/plans/approved-plan")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert repository.plans["plan-1"].status is PlanStatus.SUPERSEDED
    assert deleted_again.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["code"] == "plan_not_found"
    assert applying.status_code == 409
    assert applying.json()["code"] == "plan_stale"
    assert repository.plans["applying-plan"].status is PlanStatus.APPLYING
    assert approved.status_code == 409
    assert repository.plans["approved-plan"].status is PlanStatus.APPROVED


def test_openapi_describes_strict_typed_plan_requests_and_responses(plan_api):
    client, _, _, _, _ = plan_api

    schema = client.get("/api/v1/openapi.json").json()
    paths = schema["paths"]

    # GET lists plans by status and delivery mode (F4).
    assert set(paths["/api/v1/automation/plans"]) == {"get", "post"}
    assert set(paths["/api/v1/automation/plans/{plan_id}/patch"]) == {"get"}
    assert set(paths["/api/v1/automation/plans/{plan_id}/delivery"]) == {"post"}
    assert set(paths["/api/v1/automation/plans/{plan_id}"]) == {"get", "delete"}
    assert "204" in paths["/api/v1/automation/plans/{plan_id}"]["delete"]["responses"]
    assert set(paths["/api/v1/automation/plans/{plan_id}/rollback-preview"]) == {"post"}
    assert set(paths["/api/v1/automation/plans/{plan_id}/approve"]) == {"post"}
    assert set(paths["/api/v1/automation/plans/{plan_id}/cancel"]) == {"post"}
    create_ref = paths["/api/v1/automation/plans"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    create_schema = schema["components"]["schemas"][create_ref.rsplit("/", 1)[-1]]
    assert create_schema["additionalProperties"] is False
    assert create_schema["required"] == ["recommendation_ids"]
    assert set(create_schema["properties"]) == {"recommendation_ids", "values_mapping"}
    assert create_schema["properties"]["recommendation_ids"]["maxItems"] == 100
    assert "409" in paths["/api/v1/automation/plans/{plan_id}/approve"]["post"]["responses"]
    assert "424" in paths["/api/v1/automation/plans"]["post"]["responses"]
    assert "429" in paths["/api/v1/automation/plans"]["post"]["responses"]
    assert "503" in paths["/api/v1/automation/plans"]["post"]["responses"]


def test_openapi_keeps_typed_operations_in_action_plan_responses(plan_api):
    client, _, _, _, _ = plan_api

    schema = client.get("/api/v1/openapi.json").json()
    components = schema["components"]["schemas"]
    action_plan = next(definition for name, definition in components.items() if name.startswith("ActionPlanV3"))

    items = action_plan["properties"]["operations"]["items"]
    serialized = json.dumps(items)
    for model in ("SetContainerResources", "DeleteResource", "AddHorizontalPodAutoscaler"):
        assert f"#/components/schemas/{model}" in serialized
    assert '"propertyName": "op"' in serialized


class _Recommendations:
    def __init__(self, **overrides: Any) -> None:
        from types import SimpleNamespace

        values: dict[str, Any] = {
            "id": 42,
            "type": "RIGHTSIZING_CPU",
            "status": "active",
            "risk_level": "low",
            "confidence": 0.95,
            "current_cpu_request_millicores": 1000,
            "recommended_cpu_request_millicores": 300,
            "source": "greenkube",
            "evidence": SimpleNamespace(sample_count=100),
            "consensus": None,
            "eligibility": None,
        }
        values.update(overrides)
        self.record = SimpleNamespace(**values)

    async def get_recommendation_by_id(self, recommendation_id: int):
        return self.record if recommendation_id == 42 else None


def _enable_auto_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    from greenkube.core.config import Config

    monkeypatch.setattr(
        "greenkube.api.routers.plans.get_config",
        lambda: Config(AUTOMATION_AUTO_APPROVE_ENABLED=True),
    )


def test_create_auto_approves_an_eligible_preview_when_the_policy_allows_it(plan_api, monkeypatch):
    """F5: automation.autoApprove skips the extra click; the plan is approved and queued."""
    from greenkube.api.dependencies import get_recommendation_repository

    client, app, repository, _, queue = plan_api
    _enable_auto_approve(monkeypatch)
    app.dependency_overrides[get_recommendation_repository] = lambda: _Recommendations()

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["auto_approved"] is True and body["auto_approve_blockers"] == []
    assert body["status"] == "approved"
    assert body["operation_id"] == "operation-plan-1"
    assert len(queue.operations) == 1


def test_create_keeps_a_contested_preview_for_the_approval_click(plan_api, monkeypatch):
    from greenkube.api.dependencies import get_recommendation_repository

    client, app, _, _, queue = plan_api
    _enable_auto_approve(monkeypatch)
    app.dependency_overrides[get_recommendation_repository] = lambda: _Recommendations(
        consensus={"contested": True, "independent_votes": 3}
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "validated"
    assert response.json()["auto_approved"] is False
    assert response.json()["auto_approve_blockers"] == ["consensus_contested"]
    assert queue.operations == {}


def test_create_never_auto_approves_a_memory_decrease_with_unknown_oom_history(plan_api, monkeypatch):
    """A person may open such a PR; automation.autoApprove never does, even at maxRisk medium."""
    from greenkube.api.dependencies import get_recommendation_repository
    from greenkube.core.config import Config

    client, app, _, _, queue = plan_api
    monkeypatch.setattr(
        "greenkube.api.routers.plans.get_config",
        lambda: Config(AUTOMATION_AUTO_APPROVE_ENABLED=True, AUTOMATION_AUTO_APPROVE_MAX_RISK="medium"),
    )
    app.dependency_overrides[get_recommendation_repository] = lambda: _Recommendations(
        type="RIGHTSIZING_MEMORY",
        risk_level="medium",
        current_memory_request_bytes=2048,
        recommended_memory_request_bytes=1024,
        risk_factors=["oom_history_unknown", "oom_unknown_signal_failed"],
    )

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "validated"
    assert body["auto_approved"] is False
    assert body["auto_approve_blockers"] == ["oom_history_unknown"]
    assert queue.operations == {}


def test_create_does_not_read_recommendations_when_auto_approve_is_off(plan_api):
    client, _, _, _, queue = plan_api

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 201
    assert response.json()["auto_approved"] is False and response.json()["auto_approve_blockers"] == []
    assert queue.operations == {}


@pytest.mark.parametrize("status", ["dismissed", "applied", "expired"])
def test_create_reports_not_active_for_a_recommendation_that_is_no_longer_active(plan_api, status):
    """Planning a non-active recommendation is a lifecycle problem, not a container-target one."""
    from greenkube.api.dependencies import get_recommendation_repository

    client, app, _, service, _ = plan_api
    app.dependency_overrides[get_recommendation_repository] = lambda: _Recommendations(status=status)

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_active"
    assert service.preparation.calls == []


def test_create_reports_not_active_when_a_grouped_recommendation_is_no_longer_active(plan_api):
    from types import SimpleNamespace

    from greenkube.api.dependencies import get_recommendation_repository

    class _Grouped:
        async def get_recommendation_by_id(self, recommendation_id: int):
            if recommendation_id == 42:
                return SimpleNamespace(status="active", action=SimpleNamespace(recommendation_ids=[42, 43]))
            if recommendation_id == 43:
                return SimpleNamespace(status="dismissed", action=None)
            return None

    client, app, _, _, _ = plan_api
    app.dependency_overrides[get_recommendation_repository] = lambda: _Grouped()

    response = client.post("/api/v1/automation/plans", json={"recommendation_ids": [42]})

    assert response.status_code == 409
    assert response.json()["code"] == "not_active"
