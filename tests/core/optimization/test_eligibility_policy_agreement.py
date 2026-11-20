"""F-07: displayed eligibility and plan creation evaluate the same policy gates.

A recommendation showed ``can_open_pr: true`` while ``POST /automation/plans``
refused it with ``policy_evidence_missing`` (gate ``sample_count_coverage``).
Plan creation evaluates every recommendation of the typed action (a CPU and a
memory change of one container share it) with the automation policy evaluator;
eligibility must reach the same allow/deny decision on the same records.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from greenkube.automation.policy import AutomationPolicyConfig, AutomationPolicyEvaluator, set_history_start
from greenkube.core.config import Config
from greenkube.core.recommendation_ranking import decide_records
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationType, RiskLevel
from greenkube.storage.base_gitops_repository import WorkloadBinding

NOW = datetime.now(timezone.utc)
GIB = 1024**3
POLICY_JSON = '{"missingEvidence": "warn"}'


def _action(ids: List[int]) -> ActionPlanV3:
    return ActionPlanV3(
        action_id="action-api",
        recommendation_ids=ids,
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="api"),
        operations=[
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "300m", "memory": "256Mi"})
            )
        ],
        constraints={"container_targeted": True},
        evidence_ref="recommendation-evidence/api",
    )


def _evidence(**overrides: Any) -> RecommendationEvidence:
    values: Dict[str, Any] = dict(
        sample_count=500,
        coverage_ratio=0.95,
        confidence=0.9,
        observation_window_end=NOW - timedelta(minutes=5),
        observation_window_seconds=7 * 86400,
    )
    values.update(overrides)
    return RecommendationEvidence(**values)


def _cpu(ids: List[int], **overrides: Any) -> RecommendationRecord:
    values: Dict[str, Any] = dict(
        id=7,
        pod_name="api",
        container_name="api",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        description="reduce cpu",
        risk_level=RiskLevel.LOW,
        confidence=0.9,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        evidence=_evidence(),
        action=_action(ids),
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _memory(ids: List[int], **overrides: Any) -> RecommendationRecord:
    values: Dict[str, Any] = dict(
        id=8,
        pod_name="api",
        container_name="api",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        description="reduce memory",
        risk_level=RiskLevel.LOW,
        confidence=0.9,
        current_memory_request_bytes=GIB,
        recommended_memory_request_bytes=256 * 1024 * 1024,
        evidence=_evidence(),
        action=_action(ids),
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="src-1",
        cluster="default",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="api",
        provider_id="github",
        repo_url="https://github.com/example/gitops.git",
        ref="main",
        path="apps/api",
        controller_kind="argocd",
        controller_name="api",
        controller_namespace="argocd",
        renderer="raw",
        confidence=1.0,
        status=BindingStatus.RESOLVED,
        evidence={"rollout_in_progress": False, "controller_suspended": False, "controller_healthy": True},
        observed_at=NOW,
    )


class _GitOps:
    """Bindings and plan history: one resolved binding, no plan yet."""

    async def list_workload_bindings(self, cluster: str):
        return [
            WorkloadBinding(
                cluster="default",
                namespace="prod",
                workload_kind="Deployment",
                workload_name="api",
                status=BindingStatus.RESOLVED,
                confidence=1.0,
                source=_source(),
                observed_at=NOW,
            )
        ]

    async def list_plan_records_for_workload(self, **_: Any):
        return []


class _Recommendations:
    """The persisted recommendations plan creation reads by id."""

    def __init__(self, records: List[RecommendationRecord]):
        self.by_id = {record.id: record for record in records}

    async def get_recommendation_by_id(self, rec_id: int):
        return self.by_id.get(rec_id)

    async def get_recommendations_by_statuses(self, statuses):
        return []


CASES = {
    "one recommendation, enough evidence": lambda: [_cpu([7])],
    "one recommendation, too few samples": lambda: [_cpu([7], evidence=_evidence(sample_count=10))],
    "one recommendation, invalid coverage": lambda: [_cpu([7], evidence=_evidence(coverage_ratio=1.2))],
    "confidence only in the evidence": lambda: [_cpu([7], confidence=None)],
    "shared action, both ready": lambda: [_cpu([7, 8]), _memory([7, 8])],
    "shared action, the sibling has too few samples": lambda: [
        _cpu([7, 8]),
        _memory([7, 8], evidence=_evidence(sample_count=10)),
    ],
    "shared action, the sibling has no evidence": lambda: [_cpu([7, 8]), _memory([7, 8], evidence=None)],
    "shared action, the sibling is high risk": lambda: [_cpu([7, 8]), _memory([7, 8], risk_level=RiskLevel.HIGH)],
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_eligibility_and_plan_creation_agree(case, monkeypatch):
    monkeypatch.setenv("GITOPS_POLICY_JSON", POLICY_JSON)
    set_history_start(None, read_at=time.monotonic())
    records = CASES[case]()
    repo = _Recommendations(records)

    decided = await decide_records(
        records,
        config=Config(CLUSTER_NAME="default"),
        reco_repo=repo,
        gitops_repo=_GitOps(),
        automation_configured=True,
        now=NOW,
    )

    # Plan creation (PlanRuntime._evaluate_policy): the evaluator over every record of the action.
    evaluator = AutomationPolicyEvaluator(
        config=AutomationPolicyConfig.from_json(POLICY_JSON),
        plan_repository=_GitOps(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        step_seconds=300,
    )
    for record in decided:
        action_ids = record.action.recommendation_ids
        plan = await evaluator.evaluate([repo.by_id[rec_id] for rec_id in action_ids], _source())
        eligibility = record.eligibility
        assert eligibility["can_open_pr"] is plan.allowed, (
            f"{case}: recommendation {record.id} eligibility {eligibility['blockers']} vs plan {plan.rejections}"
        )
