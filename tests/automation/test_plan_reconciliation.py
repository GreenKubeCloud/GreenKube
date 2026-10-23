"""Tests for recovery of plans whose operation can no longer be retried."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.gitops.contracts import (
    ActionPlanV3,
    ActionStatus,
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

_NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
_DIGEST = "sha256:" + "c" * 64


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.example.test/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )


def _plan() -> Plan:
    plan_id = "plan-1"
    action = ActionPlanV3(
        action_id="action-1",
        recommendation_ids=[1],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
        operations=[SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "200m"}))],
        evidence_ref="recommendation/1",
    )
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[FileEdit(path="apps/payments/deployment.yaml", operation="update", original_sha="b" * 40, content="x")],
        diff="diff",
        rendered_diff="rendered",
        base_revision="a" * 40,
        source_revision="a" * 40,
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "d" * 64,
        ),
        digest=_DIGEST,
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=[1],
        source=_source(),
        action=action,
        file_plan=file_plan,
        status=PlanStatus.APPLYING,
        digest=_DIGEST,
        created_at=_NOW,
    )


class _Plans:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.status_calls: list[tuple[str, PlanStatus, dict[str, Any]]] = []
        self.action_calls: list[tuple[tuple[int, ...], ActionStatus, dict[str, Any]]] = []

    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]:
        return [self.plan] if status is None or self.plan.status is status else []

    async def set_plan_status(self, plan_id: str, status: PlanStatus, **kwargs: Any) -> Plan | None:
        self.status_calls.append((plan_id, status, kwargs))
        self.plan = self.plan.model_copy(update={"status": status})
        return self.plan

    async def set_recommendation_action_status(
        self,
        recommendation_ids: list[int],
        status: ActionStatus,
        **kwargs: Any,
    ) -> int:
        self.action_calls.append((tuple(recommendation_ids), status, kwargs))
        return len(recommendation_ids)


class _Operations:
    def __init__(self, operation: Operation | None) -> None:
        self.operation = operation

    async def get_by_plan_id(self, plan_id: str) -> Operation | None:
        return self.operation


def _operation(status: OperationStatus) -> Operation:
    return Operation(
        operation_id="operation-1",
        plan_id="plan-1",
        idempotency_key="plan:plan-1:" + _DIGEST,
        status=status,
        attempts=3,
    )


def _runtime(plans: _Plans, operations: _Operations, pull_requests: Any = None) -> RuntimePlanService:
    class PlanService:
        repository = plans

    worker = SimpleNamespace(
        operations=operations,
        pull_request_repository=pull_requests,
    )
    return RuntimePlanService(
        recommendation_repository=cast(Any, object()),
        binding_repository=cast(Any, object()),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=cast(Any, PlanService()),
        worker=cast(Any, worker),
    )


@pytest.mark.asyncio
async def test_exhausted_operation_fails_plan_and_releases_action():
    plans = _Plans(_plan())
    runtime = _runtime(plans, _Operations(_operation(OperationStatus.FAILED)))

    reconciled = await runtime.reconcile_exhausted_operations()

    assert [plan.status for plan in reconciled] == [PlanStatus.FAILED]
    assert plans.status_calls == [
        (
            "plan-1",
            PlanStatus.FAILED,
            {
                "expected_statuses": (PlanStatus.APPLYING,),
                "expected_digest": _DIGEST,
                "actor": "automation-reconciler",
                "reason_code": "operation_retry_exhausted",
            },
        )
    ]
    assert plans.action_calls[0][0] == (1,)
    assert plans.action_calls[0][1] is ActionStatus.FAILED


@pytest.mark.asyncio
async def test_retryable_operation_and_linked_pull_request_are_left_untouched():
    plans = _Plans(_plan())
    runtime = _runtime(plans, _Operations(_operation(OperationStatus.RUNNING)))
    assert await runtime.reconcile_exhausted_operations() == []
    assert plans.status_calls == []

    class PullRequests:
        async def get_pull_request_by_plan_id(self, plan_id: str) -> object:
            return object()

    runtime = _runtime(plans, _Operations(_operation(OperationStatus.FAILED)), PullRequests())
    assert await runtime.reconcile_exhausted_operations() == []
    assert plans.status_calls == []
