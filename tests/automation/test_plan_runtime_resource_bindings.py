"""Plan binding resolution of non-workload targets through resource bindings (H0d, H6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from greenkube.automation.actions import ACTION_KINDS
from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError
from greenkube.automation.policy import AutomationPolicyConfig
from greenkube.gitops.contracts import (
    AutomationErrorCode,
    ControllerKind,
    ResourceTarget,
)
from greenkube.gitops.discovery.inventory import (
    CLUSTER_SCOPE_NAMESPACE,
    CONTROLLER_PRUNE_EVIDENCE,
    ResourceBinding,
)
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.resource_binding_repository import StoredResourceBinding
from tests.gitops.planning.test_raw_manifest import _source  # pyrefly: ignore[missing-import]

PV = ResourceTarget(api_version="v1", kind="PersistentVolume", namespace="", name="pv-orphan")
ARGO = (ControllerKind.ARGOCD, "argocd", "payments-prod")
POLICY = AutomationPolicyConfig(missing_evidence="warn", max_risk_level="high")
#: Controller evidence discovery copies from the controller's workload binding.
HEALTHY = {"controller_suspended": False, "controller_healthy": True}


def _record(**overrides: Any) -> RecommendationRecord:
    recommendation = Recommendation(
        id=9,
        pod_name="pv-orphan",
        type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        scope="cluster",
        description="PersistentVolume 'pv-orphan' appears unused",
        risk_level="high",
        confidence=0.9,
    )
    action = ACTION_KINDS.build_action_plans([recommendation])[9]
    values: dict[str, Any] = {
        "id": 9,
        "pod_name": "pv-orphan",
        "type": RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        "scope": "cluster",
        "description": "PersistentVolume 'pv-orphan' appears unused",
        "status": RecommendationStatus.ACTIVE,
        "risk_level": "high",
        "confidence": 0.9,
        "action": action,
    }
    values.update(overrides)
    return RecommendationRecord(**values)


class _Recommendations:
    def __init__(self, record: RecommendationRecord) -> None:
        self.record = record

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
        return self.record if rec_id == self.record.id else None


class _WorkloadBindings:
    async def get_workload_binding(self, *args: Any) -> None:
        raise AssertionError("a resource-bound kind never reads workload bindings")


class _ResourceBindings:
    def __init__(self, stored: StoredResourceBinding | None) -> None:
        self.stored = stored
        self.calls: list[tuple[str, ResourceTarget]] = []

    async def get(self, cluster: str, ref: ResourceTarget) -> StoredResourceBinding | None:
        self.calls.append((cluster, ref))
        return self.stored


class _Policies:
    async def list_plans(self, *, status=None, source_id=None):
        return []

    async def get_plan_record(self, plan_id: str):
        return None


def _service(stored: StoredResourceBinding | None, record: RecommendationRecord | None = None) -> RuntimePlanService:
    return RuntimePlanService(
        recommendation_repository=cast(Any, _Recommendations(record or _record())),
        binding_repository=cast(Any, _WorkloadBindings()),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=cast(Any, object()),
        policy_config=POLICY,
        policy_repository=cast(Any, _Policies()),
        resource_binding_repository=_ResourceBindings(stored),
    )


def _now() -> datetime:
    """The wall clock at test time (never at import: a long suite would age the bindings)."""
    return datetime.now(timezone.utc)


def _stored(
    *, status: str = "resolved", location: Any = "default", age: timedelta = timedelta(0)
) -> StoredResourceBinding:
    binding = ResourceBinding(
        ref=PV,
        status=cast(Any, status),
        controllers=(ARGO,),
        location=_source(evidence=HEALTHY) if location == "default" else location,
        prune=True,
    )
    return StoredResourceBinding(binding=binding, observed_at=_now() - age)


@pytest.mark.asyncio
async def test_a_cluster_scoped_deletion_resolves_its_source_from_the_resource_binding() -> None:
    action, source = await _service(_stored())._resolve_request([9])

    assert action.target == PV
    assert (source.namespace, source.workload_kind, source.workload_name) == (
        CLUSTER_SCOPE_NAMESPACE,
        "PersistentVolume",
        "pv-orphan",
    )
    assert source.evidence[CONTROLLER_PRUNE_EVIDENCE] is True
    assert source.repo_url == _source().repo_url
    assert source.evidence["controller_healthy"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "code", "message"),
    [
        (lambda: None, AutomationErrorCode.AMBIGUOUS_BINDING, "not managed by an Argo CD Application"),
        (lambda: _stored(status="ambiguous"), AutomationErrorCode.AMBIGUOUS_BINDING, "several GitOps controllers"),
        (lambda: _stored(location=None), AutomationErrorCode.AMBIGUOUS_BINDING, "unresolved"),
        (lambda: _stored(age=timedelta(days=2)), AutomationErrorCode.SOURCE_CHANGED, "stale"),
    ],
)
async def test_unbound_ambiguous_or_stale_resources_fail_closed(stored, code, message) -> None:
    with pytest.raises(AutomationPlanError) as error:
        await _service(stored())._resolve_request([9])
    assert error.value.code is code
    assert message in error.value.detail


@pytest.mark.asyncio
async def test_a_recommendation_for_another_resource_is_refused() -> None:
    record = _record(pod_name="other-pv")
    with pytest.raises(AutomationPlanError) as error:
        await _service(_stored(), record)._resolve_request([9])
    assert error.value.code is AutomationErrorCode.AMBIGUOUS_BINDING


@pytest.mark.asyncio
async def test_the_policy_evaluates_a_deletion_with_the_production_defaults() -> None:
    """Only the risk limit is raised: no workload-only gate rejects a resource deletion."""
    from greenkube.automation.policy import AutomationPolicyEvaluator

    service = _service(_stored())
    action, source = await service._resolve_request([9])
    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig(max_risk_level="high"),
        plan_repository=cast(Any, _Policies()),
        clock=_now,
    )

    result = await evaluator.evaluate([_record()], source)

    assert result.allowed, [(item.code, item.detail) for item in result.rejections]


@pytest.mark.asyncio
async def test_an_unhealthy_controller_still_blocks_a_deletion() -> None:
    from greenkube.automation.policy import AutomationPolicyEvaluator

    unhealthy = _stored(location=_source(evidence={"controller_suspended": True, "controller_healthy": True}))
    _, source = await _service(unhealthy)._resolve_request([9])
    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig(max_risk_level="high"),
        plan_repository=cast(Any, _Policies()),
        clock=_now,
    )

    result = await evaluator.evaluate([_record()], source)

    assert not result.allowed
    assert [item.code for item in result.rejections] == [AutomationErrorCode.CONTROLLER_UNHEALTHY]
