# tests/core/optimization/test_eligibility.py
"""Server-side delivery eligibility (roadmap F10, F7)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from greenkube.automation.policy import AutomationPolicyConfig
from greenkube.core.optimization.eligibility import (
    CODES,
    EligibilityContext,
    assess_eligibility,
    criticality_by_record,
    evaluate_policies,
    load_bindings,
    load_open_pr_ids,
    load_policy,
    validate_action,
)
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    ResourceTarget,
    SetContainerResources,
    WorkloadSource,
)
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import (
    PullRequestRecord,
    PullRequestStatus,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
    RiskLevel,
)
from greenkube.storage.base_gitops_repository import WorkloadBinding

NOW = datetime.now(timezone.utc)
KEY = ("prod", "Deployment", "api")


def _action(ids=(7,), container="api", targeted=True):
    return ActionPlanV3(
        action_id="action-7",
        recommendation_ids=list(ids),
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="prod", name="api"),
        operations=[SetContainerResources(container=container, resources=ContainerResources(requests={"cpu": "300m"}))],
        constraints={"container_targeted": targeted},
        evidence_ref="recommendation-evidence/7",
    )


def _record(**overrides):
    values = dict(
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
        evidence=RecommendationEvidence(
            sample_count=500,
            coverage_ratio=0.95,
            observation_window_end=NOW - timedelta(minutes=5),
            observation_window_seconds=7 * 86400,
        ),
        action=_action(),
    )
    values.update(overrides)
    return RecommendationRecord(**values)


def _source(**evidence):
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
        evidence=evidence,
        observed_at=NOW,
    )


def _binding(status=BindingStatus.RESOLVED, reason=None, **evidence):
    source = _source(**evidence) if status is BindingStatus.RESOLVED else None
    return WorkloadBinding(
        cluster="default",
        namespace="prod",
        workload_kind="Deployment",
        workload_name="api",
        status=status,
        confidence=1.0 if source else 0.0,
        source=source,
        reason=reason,
        observed_at=NOW,
    )


def _ctx(**overrides):
    values = dict(
        now=NOW,
        automation_configured=True,
        automation_enabled=True,
        bindings={KEY: _binding()},
    )
    values.update(overrides)
    return EligibilityContext(**values)


def _codes(result):
    return [blocker["code"] for blocker in result["blockers"]]


class TestEligibility:
    def test_eligible_recommendation(self):
        result = assess_eligibility(_record(), _ctx())
        assert result["can_open_pr"] is True
        assert result["blockers"] == []
        assert result["delivery_modes"] == ["direct", "manual"]
        assert result["action_kind"] == "container_resources"
        assert result["recommendation_ids"] == [7]
        assert result["eligible_at"] is None

    def test_every_blocker_has_a_message_and_a_fix(self):
        result = assess_eligibility(
            _record(status=RecommendationStatus.IGNORED, risk_level=RiskLevel.HIGH, confidence=0.1),
            _ctx(automation_enabled=False),
        )
        assert result["can_open_pr"] is False
        assert result["delivery_modes"] == []
        for blocker in result["blockers"]:
            assert blocker["message"] and blocker["fix"], blocker
        assert set(_codes(result)) >= {"not_active", "automation_disabled", "policy_risk_exceeded"}

    def test_unsupported_action_kind_names_the_planned_kind(self):
        zombie = _record(type=RecommendationType.ZOMBIE_POD, action=None, scope="pod")
        result = assess_eligibility(zombie, _ctx())
        assert _codes(result) == ["action_kind_unsupported"]
        assert "deletion" in result["blockers"][0]["message"] and "H6" in result["blockers"][0]["message"]
        assert result["delivery_modes"] == ["manual"]

    def test_missing_action_and_unsafe_targets(self):
        assert _codes(assess_eligibility(_record(action=None), _ctx())) == ["action_unavailable"]
        untargeted = _record(action=_action(targeted=False))
        assert _codes(assess_eligibility(untargeted, _ctx())) == ["requires_container_target"]
        other_container = _record(action=_action(container="sidecar"))
        assert _codes(assess_eligibility(other_container, _ctx())) == ["requires_container_target"]
        not_in_action = _record(action=_action(ids=(8,)))
        assert _codes(assess_eligibility(not_in_action, _ctx())) == ["action_unavailable"]

    def test_change_set_ids_are_returned(self):
        ids, code = validate_action(_record(action=_action(ids=(7, 9))))
        assert (ids, code) == ([7, 9], None)

    def test_automation_not_configured(self):
        result = assess_eligibility(_record(), _ctx(automation_configured=False, bindings=None))
        assert _codes(result) == ["automation_not_configured"]

    def test_binding_states(self):
        cases = {
            None: "binding_missing",
            BindingStatus.AMBIGUOUS: "ambiguous_binding",
            BindingStatus.UNRESOLVED: "binding_unresolved",
            BindingStatus.BLOCKED: "binding_blocked",
            BindingStatus.STALE: "binding_stale",
        }
        for status, code in cases.items():
            bindings = {} if status is None else {KEY: _binding(status, reason="two sources")}
            result = assess_eligibility(_record(), _ctx(bindings=bindings))
            assert _codes(result) == [code], status
        assert _codes(assess_eligibility(_record(), _ctx(bindings=None))) == ["binding_unavailable"]

    def test_open_pull_request(self):
        result = assess_eligibility(_record(), _ctx(open_pr_recommendation_ids=frozenset({7})))
        assert _codes(result) == ["pull_request_open"]
        assert result["delivery_modes"] == ["manual"]

    def test_policy_gates(self):
        policy = AutomationPolicyConfig(max_risk_level=RiskLevel.LOW, min_confidence=0.95)
        result = assess_eligibility(_record(risk_level=RiskLevel.MEDIUM), _ctx(policy=policy))
        assert _codes(result) == ["policy_risk_exceeded", "policy_confidence_too_low"]

    def test_change_freeze_window(self):
        policy = AutomationPolicyConfig.model_validate(
            {
                "changeFreezeWindows": [
                    {"start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}
                ]
            }
        )
        assert _codes(assess_eligibility(_record(), _ctx(policy=policy))) == ["change_freeze_active"]

    def test_invalid_policy_fails_closed(self):
        policy, error = load_policy("{not json")
        assert error
        result = assess_eligibility(_record(), _ctx(policy=policy, policy_error=error))
        assert _codes(result) == ["policy_invalid"]


class TestWarmUp:
    def test_eta_from_missing_samples(self):
        evidence = RecommendationEvidence(
            sample_count=6,
            coverage_ratio=0.9,
            observation_window_end=NOW,
            observation_window_seconds=1800,
        )
        result = assess_eligibility(_record(evidence=evidence), _ctx(step_seconds=300))
        assert _codes(result) == ["warming_up"]
        eligible_at = datetime.fromisoformat(result["eligible_at"])
        assert eligible_at == NOW + timedelta(seconds=(36 - 6) * 300)
        assert "Eligible in ~3h" in result["blockers"][0]["fix"]
        assert "6/36 samples" in result["blockers"][0]["message"]

    def test_eta_from_low_coverage(self):
        evidence = RecommendationEvidence(
            sample_count=1000,
            coverage_ratio=0.2,
            observation_window_end=NOW,
            observation_window_seconds=7 * 86400,
        )
        result = assess_eligibility(_record(evidence=evidence), _ctx())
        eligible_at = datetime.fromisoformat(result["eligible_at"])
        assert eligible_at == NOW + timedelta(seconds=(0.5 - 0.2) * 7 * 86400)

    def test_past_eta_waits_for_the_next_scan(self):
        evidence = RecommendationEvidence(
            sample_count=30,
            coverage_ratio=0.9,
            observation_window_end=NOW - timedelta(days=1),
            observation_window_seconds=86400,
        )
        result = assess_eligibility(_record(evidence=evidence), _ctx())
        assert "next recommendation scan" in result["blockers"][0]["fix"]

    def test_missing_evidence(self):
        result = assess_eligibility(_record(evidence=None), _ctx())
        assert _codes(result) == ["policy_evidence_missing"]
        assert result["eligible_at"] is None


class TestWarnings:
    def test_singleton_and_memory_warnings(self):
        record = _record(risk_factors=["sufficient_headroom", "singleton_workload", "memory_decrease_best_effort"])
        result = assess_eligibility(record, _ctx())
        assert [warning["code"] for warning in result["warnings"]] == [
            "singleton_workload",
            "memory_decrease_best_effort",
        ]
        assert result["can_open_pr"] is True

    def test_documented_codes(self):
        documented = (Path(__file__).resolve().parents[3] / "docs/reference/eligibility-codes.md").read_text("utf-8")
        for code in CODES:
            assert f"`{code}`" in documented, code


class TestLiveContext:
    async def test_policy_evaluator_live_gates(self):
        """The evaluator that guards plan creation runs read-only on resolved bindings."""
        binding = _binding(rollout_in_progress=True, controller_suspended=False, controller_healthy=True)
        plans = MagicMock()
        plans.list_plan_records_for_workload = AsyncMock(return_value=[])
        ctx = _ctx(bindings={KEY: binding})
        results = await evaluate_policies([_record()], ctx, plans)
        assert 7 in results
        result = assess_eligibility(_record(), replace(ctx, policy_results=results))
        assert "rollout_in_progress" in _codes(result)
        assert result["can_open_pr"] is False

    async def test_evaluator_skips_unresolved_and_disabled(self):
        plans = MagicMock()
        ctx = _ctx(bindings={KEY: _binding(BindingStatus.AMBIGUOUS)})
        assert await evaluate_policies([_record()], ctx, plans) == {}
        assert await evaluate_policies([_record()], _ctx(automation_enabled=False), plans) == {}

    async def test_bindings_and_open_pull_requests(self):
        repo = MagicMock()
        repo.list_workload_bindings = AsyncMock(return_value=[_binding()])
        bindings = await load_bindings(repo, "default", NOW)
        assert bindings is not None and bindings[KEY].status is BindingStatus.RESOLVED
        repo.list_workload_bindings = AsyncMock(side_effect=RuntimeError("down"))
        assert await load_bindings(repo, "default", NOW) is None

        prs = MagicMock()
        prs.get_open_pull_requests = AsyncMock(
            return_value=[
                PullRequestRecord(recommendation_id=7, provider="github", repo="o/r", status=PullRequestStatus.OPEN),
                PullRequestRecord(recommendation_id=8, provider="github", repo="o/r", status=PullRequestStatus.MERGED),
            ]
        )
        assert await load_open_pr_ids(prs) == frozenset({7})
        assert await load_open_pr_ids(None) == frozenset()

    def test_criticality_from_binding_labels(self):
        ctx = _ctx(bindings={KEY: _binding(workload_labels={"greenkube.io/criticality": "critical"})})
        assert criticality_by_record([_record()], ctx) == {7: "critical"}
