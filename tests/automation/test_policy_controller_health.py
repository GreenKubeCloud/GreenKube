"""An OOM-crashlooping workload's own reliability fix is not blocked by its Degraded Argo CD app.

Before, the Degraded/Progressing health the crashloop caused tripped ``controller_unhealthy``,
so the request/limit increase that fixes it could never get a pull request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from greenkube.automation.plan_runtime import argo_application_status_reader
from greenkube.automation.policy import (
    AutomationPolicyConfig,
    AutomationPolicyEvaluator,
    AutomationPolicyResult,
    PolicyCheckState,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.evidence import LimitChange, RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationType

_DEGRADED = {
    "rollout_in_progress": False,
    "controller_suspended": False,
    "controller_healthy": False,
    "hpa_pdb_interactions_checked": True,
    "hpa_targets": [],
    "pdb_conflict": False,
    "vpa_interactions_checked": True,
    "vpa_targets": [],
}


class _EmptyHistory:
    async def list_plans_for_workload(self, **kwargs: Any) -> list:
        return []

    async def list_plans(self, **kwargs: Any) -> list:
        return []

    async def get_plan_record(self, plan_id: str):
        return None


def _source(evidence: dict[str, Any] | None = None, *, kind: ControllerKind = ControllerKind.ARGOCD) -> WorkloadSource:
    return WorkloadSource(
        id="source-api",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github-main",
        repo_url="https://github.example.test/acme/gitops.git",
        ref="main",
        path="apps/payments",
        controller_kind=kind,
        controller_name="payments",
        controller_namespace="argocd",
        controller_uid="app-uid",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
        evidence=dict(_DEGRADED if evidence is None else evidence),
    )


def _memory_record(current: int, proposed: int, limit_changes: list[LimitChange] | None = None) -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        container_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="The container is OOM-killed: raise its memory request.",
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=proposed,
        action=ActionPlanV3(
            action_id="action-1",
            recommendation_ids=[1],
            target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
            operations=[
                SetContainerResources(container="api", resources=ContainerResources(requests={"memory": f"{proposed}"}))
            ],
            evidence_ref="recommendation/1",
        ),
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=48, coverage_ratio=1.0, limit_changes=limit_changes or []),
    )


def _app_status(
    *,
    health: str = "Degraded",
    target_health: str = "Degraded",
    others: list[dict[str, Any]] | None = None,
    conditions: list[dict[str, Any]] | None = None,
    phase: str | None = "Succeeded",
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "health": {"status": health},
        "sync": {"status": "Synced"},
        "resources": [
            {"group": "", "kind": "Service", "namespace": "payments", "name": "api", "health": {"status": "Healthy"}},
            {
                "group": "apps",
                "kind": "Deployment",
                "namespace": "payments",
                "name": "api",
                "health": {"status": target_health, "message": "Deployment exceeded its progress deadline"},
            },
            {"group": "", "kind": "ConfigMap", "namespace": "payments", "name": "api-config"},
            *(others or []),
        ],
    }
    if conditions is not None:
        status["conditions"] = conditions
    if phase is not None:
        status["operationState"] = {"phase": phase}
    return status


async def _evaluate(
    record: RecommendationRecord,
    status: dict[str, Any] | None,
    *,
    source: WorkloadSource | None = None,
    reader_error: Exception | None = None,
) -> AutomationPolicyResult:
    reads: list[WorkloadSource] = []

    async def reader(selected: WorkloadSource):
        reads.append(selected)
        if reader_error is not None:
            raise reader_error
        return status

    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig.model_validate({"missingEvidence": "fail"}),
        plan_repository=_EmptyHistory(),
        clock=lambda: datetime.now(timezone.utc),
        controller_status_reader=reader,
    )
    return await evaluator.evaluate([record], source or _source())


def _controller_check(result: AutomationPolicyResult):
    return next(check for check in result.checks if check.gate == "controller_health")


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["Degraded", "Progressing"])
async def test_increase_only_fix_is_not_blocked_by_its_own_workloads_health(health):
    result = await _evaluate(_memory_record(128 * 2**20, 256 * 2**20), _app_status(health=health, target_health=health))

    check = _controller_check(result)
    assert check.state is PolicyCheckState.PASSED
    assert "only because the target Deployment payments/api" in check.message
    assert "increase-only reliability fix" in check.message
    assert check.observed["self_caused"] is True
    assert AutomationErrorCode.CONTROLLER_UNHEALTHY not in {rejection.code for rejection in result.rejections}


@pytest.mark.asyncio
async def test_a_raised_limit_with_the_request_still_qualifies():
    record = _memory_record(
        128 * 2**20,
        256 * 2**20,
        [LimitChange(resource="memory", action="set", current=200 * 2**20, proposed=384 * 2**20)],
    )
    result = await _evaluate(record, _app_status())
    assert _controller_check(result).state is PolicyCheckState.PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        pytest.param(lambda: _memory_record(256 * 2**20, 128 * 2**20), id="decrease"),
        pytest.param(
            lambda: _memory_record(
                128 * 2**20,
                256 * 2**20,
                [LimitChange(resource="memory", action="set", current=512 * 2**20, proposed=300 * 2**20)],
            ),
            id="lowered-limit",
        ),
    ],
)
async def test_plans_that_are_not_increase_only_stay_blocked(record):
    result = await _evaluate(record(), _app_status())
    assert _controller_check(result).state is PolicyCheckState.REJECTED
    assert result.rejections[0].code is AutomationErrorCode.CONTROLLER_UNHEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        pytest.param(
            _app_status(
                others=[
                    {
                        "group": "apps",
                        "kind": "Deployment",
                        "namespace": "payments",
                        "name": "worker",
                        "health": {"status": "Degraded"},
                    }
                ]
            ),
            id="another-resource-degraded",
        ),
        pytest.param(_app_status(conditions=[{"type": "SyncError", "message": "x"}]), id="sync-error"),
        pytest.param(_app_status(conditions=[{"type": "ComparisonError", "message": "x"}]), id="comparison-error"),
        pytest.param(_app_status(phase="Failed"), id="failed-sync-operation"),
        pytest.param(_app_status(health="Missing"), id="missing-app"),
        pytest.param(_app_status(target_health="Healthy"), id="unexplained"),
        pytest.param(_app_status(target_health="Missing"), id="target-missing"),
        pytest.param(None, id="status-unavailable"),
    ],
)
async def test_other_controller_problems_still_block(status):
    result = await _evaluate(_memory_record(128 * 2**20, 256 * 2**20), status)
    assert _controller_check(result).state is PolicyCheckState.REJECTED
    assert result.rejections[0].code is AutomationErrorCode.CONTROLLER_UNHEALTHY


@pytest.mark.asyncio
async def test_suspended_controller_and_non_argo_controllers_still_block():
    suspended = await _evaluate(
        _memory_record(128 * 2**20, 256 * 2**20),
        _app_status(),
        source=_source({**_DEGRADED, "controller_suspended": True}),
    )
    assert _controller_check(suspended).state is PolicyCheckState.REJECTED

    flux = await _evaluate(
        _memory_record(128 * 2**20, 256 * 2**20),
        _app_status(),
        source=_source(kind=ControllerKind.FLUX_KUSTOMIZATION),
    )
    assert _controller_check(flux).state is PolicyCheckState.REJECTED

    failing_reader = await _evaluate(
        _memory_record(128 * 2**20, 256 * 2**20), _app_status(), reader_error=RuntimeError("api down")
    )
    assert _controller_check(failing_reader).state is PolicyCheckState.REJECTED


class _CustomObjects:
    def __init__(self, application: Any) -> None:
        self.application = application
        self.calls: list[tuple[Any, ...]] = []

    async def get_namespaced_custom_object(self, *args: Any) -> Any:
        self.calls.append(args)
        if isinstance(self.application, Exception):
            raise self.application
        return self.application


@pytest.mark.asyncio
async def test_argo_status_reader_reads_the_bound_application_only():
    status = _app_status()
    api = _CustomObjects({"metadata": {"uid": "app-uid"}, "status": status})
    reader = argo_application_status_reader(api)

    assert await reader(_source()) == status
    assert api.calls == [("argoproj.io", "v1alpha1", "argocd", "applications", "payments")]

    # A recreated Application (another UID) is not the bound controller.
    assert (
        await argo_application_status_reader(_CustomObjects({"metadata": {"uid": "other"}, "status": status}))(
            _source()
        )
        is None
    )
    assert await argo_application_status_reader(_CustomObjects(RuntimeError("forbidden")))(_source()) is None
    assert await reader(_source(kind=ControllerKind.FLUX_KUSTOMIZATION)) is None


def _rollout_check(result: AutomationPolicyResult):
    return next(check for check in result.checks if check.gate == "rollout_state")


# The live status of an OOM-killed Deployment (e2e): its rollout is complete, its pod is not ready.
_CRASHLOOPING = {**_DEGRADED, "rollout_in_progress": True, "rollout_pods_unavailable_only": True}


@pytest.mark.asyncio
async def test_the_rollout_gate_lets_the_fix_of_a_crashlooping_workload_through():
    result = await _evaluate(_memory_record(128 * 2**20, 256 * 2**20), _app_status(), source=_source(_CRASHLOOPING))

    assert _rollout_check(result).state is PolicyCheckState.PASSED
    assert _controller_check(result).state is PolicyCheckState.PASSED
    assert result.rejections == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "record"),
    [
        pytest.param(_CRASHLOOPING, lambda: _memory_record(256 * 2**20, 128 * 2**20), id="decrease"),
        pytest.param(
            {**_CRASHLOOPING, "rollout_pods_unavailable_only": False},
            lambda: _memory_record(128 * 2**20, 256 * 2**20),
            id="real-rollout",
        ),
    ],
)
async def test_the_rollout_gate_still_blocks_other_plans(evidence, record):
    result = await _evaluate(record(), _app_status(), source=_source(evidence))

    assert _rollout_check(result).state is PolicyCheckState.REJECTED
    assert AutomationErrorCode.ROLLOUT_IN_PROGRESS in {rejection.code for rejection in result.rejections}
