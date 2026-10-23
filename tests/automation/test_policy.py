"""Focused tests for GitOps plan policy decisions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from greenkube.automation.policy import (
    AutomationPolicyConfig,
    AutomationPolicyEvaluator,
    PolicyCheckState,
)
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
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.storage.base_gitops_repository import AutomationPlanRecord

_NOW = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)


def _source(*, workload_name: str = "api", evidence: dict[str, Any] | None = None) -> WorkloadSource:
    return WorkloadSource(
        id=f"source-{workload_name}",
        cluster="prod",
        namespace="payments",
        workload_kind="Deployment",
        workload_name=workload_name,
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
        evidence=evidence or {},
    )


def _recommendation(
    recommendation_id: int = 1,
    *,
    risk_level: str | None = "low",
    confidence: float | None = 0.9,
) -> RecommendationRecord:
    return RecommendationRecord(
        id=recommendation_id,
        namespace="payments",
        owner_kind="Deployment",
        owner_name="api",
        scope="workload",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce workload requests.",
        risk_level=risk_level,
        confidence=confidence,
        evidence=RecommendationEvidence(sample_count=36, coverage_ratio=1.0),
    )


def _plan(
    source: WorkloadSource,
    *,
    plan_id: str,
    status: PlanStatus,
) -> Plan:
    action = ActionPlanV3(
        action_id=plan_id,
        recommendation_ids=[1],
        target=ResourceTarget(
            api_version="apps/v1",
            kind=source.workload_kind,
            namespace=source.namespace,
            name=source.workload_name,
        ),
        operations=[
            SetContainerResources(
                container="api",
                resources=ContainerResources(requests={"cpu": "200m"}),
            )
        ],
        evidence_ref="recommendation/1",
    )
    digest = "sha256:" + "c" * 64
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
        diff="",
        rendered_diff="",
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
        digest=digest,
    )
    return Plan(
        plan_id=plan_id,
        recommendation_ids=[1],
        source=source,
        action=action,
        file_plan=file_plan,
        status=status,
        digest=digest,
        created_at=_NOW - timedelta(hours=2),
    )


class _PlanHistory:
    def __init__(
        self,
        plans: tuple[Plan, ...] = (),
        *,
        updated_at: dict[str, datetime] | None = None,
    ) -> None:
        self.plans = list(plans)
        self.updated_at = updated_at or {}

    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]:
        return [
            plan
            for plan in self.plans
            if (status is None or plan.status is status) and (source_id is None or plan.source.id == source_id)
        ]

    async def get_plan_record(self, plan_id: str) -> AutomationPlanRecord | None:
        plan = next((item for item in self.plans if item.plan_id == plan_id), None)
        if plan is None:
            return None
        return AutomationPlanRecord(plan=plan, updated_at=self.updated_at.get(plan_id, plan.created_at))


def _evaluator(
    *,
    config: AutomationPolicyConfig | None = None,
    history: _PlanHistory | None = None,
) -> AutomationPolicyEvaluator:
    return AutomationPolicyEvaluator(
        config=config or AutomationPolicyConfig(),
        plan_repository=history if history is not None else _PlanHistory(),
        clock=lambda: _NOW,
    )


def _codes(result) -> list[AutomationErrorCode]:
    return [rejection.code for rejection in result.rejections]


def test_policy_defaults_match_the_documented_gitops_values():
    config = AutomationPolicyConfig()

    assert config.max_risk_level.value == "medium"
    assert config.min_confidence == 0.6
    assert config.min_samples == 36
    assert config.min_coverage == 0.5
    assert config.max_open_pull_requests_per_workload == 1
    assert config.cooldown_minutes == 60
    assert config.change_freeze_labels == ()
    assert config.change_freeze_windows == ()


def test_policy_json_accepts_configured_timezone_aware_freeze_windows():
    config = AutomationPolicyConfig.from_json(
        json.dumps(
            {
                "changeFreezeLabels": ["maintenance=true"],
                "minSamples": 4,
                "minCoverage": 0.25,
                "changeFreezeWindows": [
                    {
                        "start": "2026-10-23T00:30:00+00:00",
                        "end": "2026-10-23T01:30:00+00:00",
                        "reason": "release window",
                    }
                ],
            }
        )
    )

    assert config.change_freeze_labels == ("maintenance=true",)
    assert config.min_samples == 4
    assert config.min_coverage == 0.25
    assert config.change_freeze_windows[0].start == datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="timezone"):
        AutomationPolicyConfig.from_json(
            json.dumps({"changeFreezeWindows": [{"start": "2026-10-23T00:30:00", "end": "2026-10-23T01:30:00"}]})
        )


@pytest.mark.asyncio
async def test_medium_risk_and_confidence_at_minimum_are_allowed():
    result = await _evaluator().evaluate(
        [_recommendation(risk_level="medium", confidence=0.6)],
        _source(),
    )

    assert result.allowed
    assert not result.rejections
    assert all(
        check.state in {PolicyCheckState.PASSED, PolicyCheckState.NOT_CONFIGURED, PolicyCheckState.NOT_AVAILABLE}
        for check in result.checks
    )
    sample_check = next(check for check in result.checks if check.gate == "sample_count_coverage")
    assert sample_check.state is PolicyCheckState.PASSED


@pytest.mark.parametrize(
    ("sample_count", "coverage_ratio"),
    [(35, 1.0), (36, 0.49)],
)
@pytest.mark.asyncio
async def test_sample_or_coverage_below_policy_threshold_rejects(sample_count, coverage_ratio):
    recommendation = _recommendation().model_copy(
        update={
            "evidence": RecommendationEvidence(
                sample_count=sample_count,
                coverage_ratio=coverage_ratio,
            )
        }
    )

    result = await _evaluator().evaluate([recommendation], _source())

    assert not result.allowed
    assert AutomationErrorCode.POLICY_EVIDENCE_MISSING in _codes(result)
    sample_check = next(check for check in result.checks if check.gate == "sample_count_coverage")
    assert sample_check.state is PolicyCheckState.REJECTED


@pytest.mark.asyncio
async def test_missing_sample_or_coverage_evidence_fails_closed():
    recommendation = _recommendation().model_copy(update={"evidence": None})

    result = await _evaluator().evaluate([recommendation], _source())

    assert not result.allowed
    assert AutomationErrorCode.POLICY_EVIDENCE_MISSING in _codes(result)


@pytest.mark.parametrize(
    ("risk_level", "confidence", "expected_code"),
    [
        ("high", 0.9, AutomationErrorCode.POLICY_RISK_EXCEEDED),
        ("low", 0.59, AutomationErrorCode.POLICY_CONFIDENCE_TOO_LOW),
    ],
)
@pytest.mark.asyncio
async def test_risk_and_confidence_thresholds_reject_with_typed_reasons(
    risk_level,
    confidence,
    expected_code,
):
    result = await _evaluator().evaluate(
        [_recommendation(risk_level=risk_level, confidence=confidence)],
        _source(),
    )

    assert not result.allowed
    assert expected_code in _codes(result)


@pytest.mark.asyncio
async def test_missing_risk_or_confidence_evidence_is_rejected():
    result = await _evaluator().evaluate(
        [_recommendation(risk_level=None, confidence=None).model_copy(update={"evidence": None})],
        _source(),
    )

    assert not result.allowed
    assert _codes(result).count(AutomationErrorCode.POLICY_EVIDENCE_MISSING) >= 2


@pytest.mark.asyncio
async def test_default_risk_in_evidence_does_not_replace_missing_policy_risk():
    recommendation = _recommendation(risk_level=None, confidence=0.9).model_copy(
        update={"evidence": RecommendationEvidence(confidence=0.9)}
    )

    result = await _evaluator().evaluate([recommendation], _source())

    assert AutomationErrorCode.POLICY_EVIDENCE_MISSING in _codes(result)


@pytest.mark.asyncio
async def test_open_pull_request_budget_is_per_workload():
    source = _source()
    open_plan = _plan(source, plan_id="open-plan", status=PlanStatus.PR_OPEN)
    other_workload_plan = _plan(_source(workload_name="worker"), plan_id="worker-plan", status=PlanStatus.PR_OPEN)
    history = _PlanHistory(
        (open_plan, other_workload_plan),
        updated_at={
            "open-plan": _NOW - timedelta(hours=2),
            "worker-plan": _NOW - timedelta(hours=2),
        },
    )

    result = await _evaluator(history=history).evaluate([_recommendation()], source)

    assert AutomationErrorCode.WORKLOAD_PR_LIMIT_EXCEEDED in _codes(result)
    open_check = next(check for check in result.checks if check.gate == "open_pull_request_budget")
    assert open_check.observed == 1


@pytest.mark.asyncio
async def test_cooldown_uses_most_recent_plan_lifecycle_activity():
    source = _source()
    merged_plan = _plan(source, plan_id="merged-plan", status=PlanStatus.MERGED)

    inside = await _evaluator(
        history=_PlanHistory(
            (merged_plan,),
            updated_at={"merged-plan": _NOW - timedelta(minutes=59)},
        )
    ).evaluate([_recommendation()], source)
    elapsed = await _evaluator(
        history=_PlanHistory(
            (merged_plan,),
            updated_at={"merged-plan": _NOW - timedelta(minutes=61)},
        )
    ).evaluate([_recommendation()], source)

    assert AutomationErrorCode.WORKLOAD_COOLDOWN_ACTIVE in _codes(inside)
    assert elapsed.allowed


@pytest.mark.asyncio
async def test_active_change_freeze_window_rejects_plan():
    config = AutomationPolicyConfig(
        change_freeze_windows=(
            {
                "start": _NOW - timedelta(minutes=1),
                "end": _NOW + timedelta(minutes=1),
                "reason": "release window",
            },
        )
    )

    result = await _evaluator(config=config).evaluate([_recommendation()], _source())

    assert AutomationErrorCode.CHANGE_FREEZE_ACTIVE in _codes(result)


@pytest.mark.asyncio
async def test_configured_freeze_label_rejects_match_and_missing_label_evidence():
    config = AutomationPolicyConfig(change_freeze_labels=("maintenance=true",))
    matching = await _evaluator(config=config).evaluate(
        [_recommendation()],
        _source(evidence={"workload_labels": {"maintenance": "true"}}),
    )
    missing = await _evaluator(config=config).evaluate(
        [_recommendation()],
        _source(),
    )

    assert AutomationErrorCode.CHANGE_FREEZE_ACTIVE in _codes(matching)
    assert AutomationErrorCode.POLICY_EVIDENCE_MISSING in _codes(missing)


@pytest.mark.parametrize(
    ("evidence", "expected_code"),
    [
        ({"rollout_in_progress": True}, AutomationErrorCode.ROLLOUT_IN_PROGRESS),
        (
            {"controller_suspended": True, "controller_healthy": False},
            AutomationErrorCode.CONTROLLER_UNHEALTHY,
        ),
        ({"hpa_pdb_conflict": True}, AutomationErrorCode.POLICY_INTERACTION_CONFLICT),
    ],
)
@pytest.mark.asyncio
async def test_available_rollout_controller_and_interaction_evidence_blocks(evidence, expected_code):
    result = await _evaluator().evaluate([_recommendation()], _source(evidence=evidence))

    assert expected_code in _codes(result)


@pytest.mark.asyncio
async def test_missing_plan_history_fails_closed_for_budget_and_cooldown():
    result = await AutomationPolicyEvaluator(
        config=AutomationPolicyConfig(),
        plan_repository=None,
        clock=lambda: _NOW,
    ).evaluate([_recommendation()], _source())

    assert not result.allowed
    assert _codes(result) == [AutomationErrorCode.POLICY_EVIDENCE_MISSING]
    assert {check.gate for check in result.checks if check.state is PolicyCheckState.REJECTED} == {
        "open_pull_request_budget",
        "active_plan",
        "cooldown",
    }
