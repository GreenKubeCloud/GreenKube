"""Truthful live policy gates: missing evidence, HPA per resource and VPA writers (D13, BUG-55)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from greenkube.automation.policy import (
    AutomationPolicyConfig,
    AutomationPolicyEvaluator,
    AutomationPolicyResult,
    PolicyCheckState,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AddHorizontalPodAutoscaler,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationType

_NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
_HEALTHY = {
    "rollout_in_progress": False,
    "controller_suspended": False,
    "controller_healthy": True,
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


def _source(evidence: dict[str, Any]) -> WorkloadSource:
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
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
        evidence=evidence,
    )


def _recommendation(*operations) -> RecommendationRecord:
    return RecommendationRecord(
        id=1,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Right-size the workload.",
        action=ActionPlanV3(
            action_id="action-1",
            recommendation_ids=[1],
            target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name="api"),
            operations=list(operations),
            evidence_ref="recommendation/1",
        ),
        risk_level="low",
        confidence=0.9,
        evidence=RecommendationEvidence(sample_count=48, coverage_ratio=1.0),
    )


def _cpu_change(container: str = "api") -> SetContainerResources:
    return SetContainerResources(container=container, resources=ContainerResources(requests={"cpu": "200m"}))


def _memory_change(container: str = "api") -> SetContainerResources:
    return SetContainerResources(container=container, resources=ContainerResources(requests={"memory": "256Mi"}))


async def _evaluate(
    evidence: dict[str, Any],
    *operations,
    missing_evidence: str = "warn",
) -> AutomationPolicyResult:
    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig.model_validate({"missingEvidence": missing_evidence}),
        plan_repository=_EmptyHistory(),
        clock=lambda: _NOW,
    )
    return await evaluator.evaluate([_recommendation(*(operations or (_cpu_change(),)))], _source(evidence))


def _check(result: AutomationPolicyResult, gate: str):
    return next(check for check in result.checks if check.gate == gate)


@pytest.mark.asyncio
async def test_complete_healthy_evidence_passes_every_live_gate():
    result = await _evaluate(dict(_HEALTHY), missing_evidence="fail")

    assert result.allowed
    for gate in ("rollout_state", "controller_health", "hpa_interaction", "vpa_interaction", "pdb_interaction"):
        assert _check(result, gate).state is PolicyCheckState.PASSED
    assert result.warnings == []


@pytest.mark.asyncio
async def test_missing_live_evidence_fails_closed_in_fail_mode():
    """The docs promise fail-closed gates; NOT_AVAILABLE used to pass silently."""
    result = await _evaluate({}, missing_evidence="fail")

    assert not result.allowed
    rejected = {check.gate for check in result.checks if check.state is PolicyCheckState.REJECTED}
    assert rejected == {"rollout_state", "controller_health", "hpa_vpa_pdb_interactions"}
    assert {rejection.code for rejection in result.rejections} == {AutomationErrorCode.POLICY_EVIDENCE_MISSING}

    partial = await _evaluate({**_HEALTHY, "vpa_interactions_checked": False}, missing_evidence="fail")
    assert not partial.allowed
    assert _check(partial, "vpa_interaction").state is PolicyCheckState.REJECTED


@pytest.mark.asyncio
async def test_missing_live_evidence_is_allowed_with_warnings_in_warn_mode():
    result = await _evaluate({}, missing_evidence="warn")

    assert result.allowed
    assert _check(result, "rollout_state").state is PolicyCheckState.NOT_AVAILABLE
    assert "missingEvidence=warn" in _check(result, "rollout_state").message
    assert len(result.warnings) == 3
    assert {warning.code for warning in result.warnings} == {AutomationErrorCode.POLICY_EVIDENCE_MISSING}
    assert result.as_dict()["policy"]["missingEvidence"] == "warn"


@pytest.mark.asyncio
async def test_cpu_utilization_hpa_blocks_cpu_changes_but_not_memory_changes():
    hpa = {
        "name": "api-hpa",
        "metric_types": ["Resource"],
        "resource_metrics": [{"resource": "cpu", "container": None, "target_type": "Utilization"}],
    }
    evidence = {**_HEALTHY, "hpa_targets": [hpa]}

    cpu = await _evaluate(evidence, _cpu_change())
    memory = await _evaluate(evidence, _memory_change())

    assert not cpu.allowed
    assert _check(cpu, "hpa_interaction").state is PolicyCheckState.REJECTED
    assert [rejection.code for rejection in cpu.rejections] == [AutomationErrorCode.POLICY_INTERACTION_CONFLICT]
    assert _check(cpu, "hpa_interaction").observed["conflicts"] == [{"hpa": "api-hpa", "resources": ["cpu"]}]
    assert memory.allowed
    assert _check(memory, "hpa_interaction").state is PolicyCheckState.PASSED
    assert [warning.code for warning in memory.warnings] == [AutomationErrorCode.POLICY_INTERACTION_CONFLICT]


@pytest.mark.asyncio
async def test_container_resource_hpa_only_blocks_its_own_container():
    hpa = {
        "name": "sidecar-hpa",
        "metric_types": ["ContainerResource"],
        "resource_metrics": [{"resource": "memory", "container": "sidecar", "target_type": "AverageValue"}],
    }
    evidence = {**_HEALTHY, "hpa_targets": [hpa]}

    api_memory = await _evaluate(evidence, _memory_change("api"))
    sidecar_memory = await _evaluate(evidence, _memory_change("sidecar"))

    assert api_memory.allowed
    assert not sidecar_memory.allowed


@pytest.mark.asyncio
async def test_adding_an_hpa_conflicts_with_an_existing_one():
    hpa = {"name": "queue-hpa", "metric_types": ["External"], "resource_metrics": []}
    result = await _evaluate(
        {**_HEALTHY, "hpa_targets": [hpa]},
        AddHorizontalPodAutoscaler(min_replicas=2, max_replicas=4),
    )

    assert not result.allowed
    assert _check(result, "hpa_interaction").state is PolicyCheckState.REJECTED


@pytest.mark.parametrize(
    ("update_mode", "allowed", "warned"),
    [
        ("Auto", False, False),
        ("Recreate", False, False),
        ("InPlaceOrRecreate", False, False),
        ("InPlace", False, False),
        ("Initial", True, True),
        ("Off", True, False),
    ],
)
@pytest.mark.asyncio
async def test_vpa_in_an_active_update_mode_is_a_conflicting_writer(update_mode, allowed, warned):
    result = await _evaluate({**_HEALTHY, "vpa_targets": [{"name": "api-vpa", "update_mode": update_mode}]})

    assert result.allowed is allowed
    expected_state = PolicyCheckState.PASSED if allowed else PolicyCheckState.REJECTED
    assert _check(result, "vpa_interaction").state is expected_state
    assert bool(result.warnings) is warned


@pytest.mark.asyncio
async def test_blocking_pdb_rejects_the_plan():
    result = await _evaluate({**_HEALTHY, "pdb_conflict": True})

    assert not result.allowed
    assert _check(result, "pdb_interaction").state is PolicyCheckState.REJECTED


@pytest.mark.asyncio
async def test_bindings_discovered_before_the_split_keep_the_aggregated_gate():
    conflict = await _evaluate({"hpa_pdb_interactions_checked": True, "hpa_pdb_conflict": True})
    clear = await _evaluate({"hpa_pdb_interactions_checked": True, "hpa_pdb_conflict": False})

    assert _check(conflict, "hpa_vpa_pdb_interactions").state is PolicyCheckState.REJECTED
    assert _check(clear, "hpa_vpa_pdb_interactions").state is PolicyCheckState.PASSED


def test_missing_evidence_setting_parses_from_policy_json_and_is_validated():
    assert AutomationPolicyConfig().missing_evidence == "warn"
    assert AutomationPolicyConfig.from_json(json.dumps({"missingEvidence": "fail"})).missing_evidence == "fail"
    assert (
        AutomationPolicyConfig.from_json(json.dumps({"missingEvidence": "fail"})).public_values()["missingEvidence"]
        == "fail"
    )
    with pytest.raises(ValidationError):
        AutomationPolicyConfig.from_json(json.dumps({"missingEvidence": "ignore"}))
