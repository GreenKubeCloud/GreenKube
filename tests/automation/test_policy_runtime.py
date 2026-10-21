"""Integration-focused tests for policy enforcement in plan runtime entry points."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_gitops_repository import AutomationPlanRecord, WorkloadBinding

_NOW = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
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


def _action() -> ActionPlanV3:
    return ActionPlanV3(
        action_id="action-1",
        recommendation_ids=[1],
        target=ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "200m"}),
            )
        ],
        evidence_ref="recommendation/1",
    )


def _plan(source: WorkloadSource, *, plan_id: str, status: PlanStatus) -> Plan:
    file_plan = FilePlan(
        plan_id=plan_id,
        files=[
            FileEdit(
                path="apps/payments/deployment.yaml",
                operation="update",
                original_sha="b" * 40,
                content="updated",
            )
        ],
        diff="diff",
        rendered_diff="rendered diff",
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
        source=source,
        action=_action(),
        file_plan=file_plan,
        status=status,
        digest=_DIGEST,
        policy_result={"version": 1, "allowed": True, "checks": [], "rejections": []},
        created_at=_NOW - timedelta(hours=2),
    )


def _recommendation(*, risk_level: str = "low", confidence: float = 0.9) -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU request.",
        action=_action(),
        status=RecommendationStatus.ACTIVE,
        risk_level=risk_level,
        confidence=confidence,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )


class _History:
    def __init__(self, plans: list[Plan] | None = None) -> None:
        self.plans = plans or []

    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]:
        return [
            plan
            for plan in self.plans
            if (status is None or plan.status is status) and (source_id is None or plan.source.id == source_id)
        ]

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None:
        plan = next((item for item in self.plans if item.plan_id == plan_id), None)
        return AutomationPlanRecord(plan=plan, updated_at=plan.created_at) if plan is not None else None


class _Recommendations:
    def __init__(self, recommendation: RecommendationRecord) -> None:
        self.recommendation = recommendation

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
        return self.recommendation if self.recommendation.id == rec_id else None


class _Bindings:
    def __init__(self, source: WorkloadSource) -> None:
        self.source = source

    async def get_workload_binding(self, cluster: str, namespace: str, workload_kind: str, workload_name: str):
        return WorkloadBinding(
            cluster=cluster,
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
            status=BindingStatus.RESOLVED,
            confidence=1,
            source=self.source,
        )


class _SnapshotResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def create_snapshot(self, source: WorkloadSource):
        self.calls += 1

        class Snapshot:
            def close(self) -> None:
                pass

        return Snapshot()


@pytest.mark.parametrize("operation", ["preview_plan", "prepare_plan"])
@pytest.mark.asyncio
async def test_plan_runtime_blocks_high_risk_before_snapshot_or_renderer(operation):
    source = _source()
    history = _History()
    snapshot_resolver = _SnapshotResolver()

    class PlanService:
        repository = history
        called = False

        async def create_plan(self, *args, **kwargs):
            self.called = True
            return None

        async def preview_plan(self, *args, **kwargs):
            self.called = True
            return None

    plan_service = PlanService()
    runtime = RuntimePlanService(
        recommendation_repository=_Recommendations(_recommendation(risk_level="high")),
        binding_repository=_Bindings(source),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshot_resolver),
        plan_service=cast(Any, plan_service),
        policy_clock=lambda: _NOW,
    )

    with pytest.raises(AutomationPlanError) as error:
        await getattr(runtime, operation)([1])

    assert error.value.code is AutomationErrorCode.POLICY_RISK_EXCEEDED
    assert error.value.policy_result is not None
    assert not error.value.policy_result["allowed"]
    assert snapshot_resolver.calls == 0
    assert not plan_service.called


@pytest.mark.asyncio
async def test_approval_revalidates_and_persists_a_new_open_pr_rejection():
    source = _source()
    current_plan = _plan(source, plan_id="current-plan", status=PlanStatus.VALIDATED)
    newly_opened_pr = _plan(source, plan_id="open-pr", status=PlanStatus.PR_OPEN)
    history = _History([current_plan, newly_opened_pr])

    class PlanService:
        repository = history
        policy_update: dict[str, Any] | None = None
        approved = False

        async def read_plan(self, plan_id: str) -> Plan | None:
            return current_plan if plan_id == current_plan.plan_id else None

        async def update_policy_result(
            self,
            plan_id: str,
            policy_result: dict[str, Any],
            *,
            expected_digest: str,
            expected_statuses,
            actor: str,
        ) -> Plan | None:
            assert plan_id == current_plan.plan_id
            assert expected_digest == _DIGEST
            assert PlanStatus.VALIDATED in expected_statuses
            self.policy_update = policy_result
            updated = current_plan.model_copy(update={"policy_result": policy_result})
            history.plans[0] = updated
            return updated

        async def approve_plan(self, plan_id: str, digest: str, *, actor: str) -> Plan:
            self.approved = True
            return current_plan

    plan_service = PlanService()
    runtime = RuntimePlanService(
        recommendation_repository=_Recommendations(_recommendation()),
        binding_repository=_Bindings(source),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=cast(Any, plan_service),
        policy_clock=lambda: _NOW,
    )

    with pytest.raises(AutomationPlanError) as error:
        await runtime.approve_plan(current_plan.plan_id, _DIGEST, actor="test")

    assert error.value.code is AutomationErrorCode.WORKLOAD_PR_LIMIT_EXCEEDED
    assert error.value.policy_result is not None
    assert error.value.policy_result["allowed"] is False
    assert plan_service.policy_update == error.value.policy_result
    assert history.plans[0].policy_result == error.value.policy_result
    assert not plan_service.approved
