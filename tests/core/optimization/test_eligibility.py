# tests/core/optimization/test_eligibility.py
"""Server-side delivery eligibility (roadmap F10, F7)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    proposes_no_change,
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
    RETIRED_RECOMMENDATION_TYPES,
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

    @pytest.mark.parametrize("mode", ["patch", "ci"])
    def test_delivery_modes_name_the_configured_delivery_mode(self, mode):
        """F4: an eligible recommendation is delivered by patch or by CI when so configured."""
        result = assess_eligibility(_record(), _ctx(delivery_mode=mode))
        assert result["can_open_pr"] is True
        assert result["delivery_modes"] == [mode, "manual"]

    def test_contested_consensus_is_a_warning_that_names_the_disagreement(self):
        contested = {"policy": "safest", "contested": True, "reasons": ["agreement 0.25 is below 0.5"]}
        result = assess_eligibility(_record(consensus=contested), _ctx())
        assert result["can_open_pr"] is True
        [warning] = [item for item in result["warnings"] if item["code"] == "consensus_contested"]
        assert "agreement 0.25 is below 0.5" in warning["message"]
        assert warning["fix"]

        agreed = assess_eligibility(_record(consensus={"policy": "safest", "contested": False}), _ctx())
        assert "consensus_contested" not in [item["code"] for item in agreed["warnings"]]

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

    @pytest.mark.parametrize("rec_type", sorted(RETIRED_RECOMMENDATION_TYPES, key=lambda item: item.value))
    def test_a_stored_record_of_a_retired_type_can_never_open_a_pull_request(self, rec_type):
        record = _record(type=rec_type, action=None, scope="workload")
        result = assess_eligibility(record, _ctx())
        assert result["can_open_pr"] is False
        assert _codes(result) == ["action_kind_unsupported"]
        assert result["blockers"][0]["message"] and result["blockers"][0]["fix"]
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

    def test_no_effective_change_blocks_a_proposal_equal_to_the_current_request(self):
        """A PR whose proposal equals the live request would change nothing (fail closed)."""
        same = _record(current_cpu_request_millicores=300, recommended_cpu_request_millicores=300)
        result = assess_eligibility(same, _ctx())
        assert _codes(result) == ["no_effective_change"]
        assert result["can_open_pr"] is False

        # 1Gi and 1024Mi are the same request: the rounded action quantity equals the current value.
        gib = 1024 * 1024 * 1024
        memory_action = _action().model_copy(
            update={
                "operations": [
                    SetContainerResources(container="api", resources=ContainerResources(requests={"memory": "1024Mi"}))
                ]
            }
        )
        rounded = _record(
            type=RecommendationType.RIGHTSIZING_MEMORY,
            current_cpu_request_millicores=None,
            recommended_cpu_request_millicores=None,
            current_memory_request_bytes=gib,
            recommended_memory_request_bytes=gib - 300 * 1024,
            action=memory_action,
        )
        assert "no_effective_change" in _codes(assess_eligibility(rounded, _ctx()))

        assert "no_effective_change" not in _codes(assess_eligibility(_record(), _ctx()))

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

    async def test_an_active_plan_is_a_warning_not_a_blocker(self):
        """An unapproved preview of the workload must not hide the preview action (D6)."""
        from greenkube.automation.policy import AutomationPolicyResult, PolicyCheck, PolicyCheckState, PolicyRejection
        from greenkube.gitops.contracts import AutomationErrorCode

        result = AutomationPolicyResult(
            allowed=False,
            evaluated_at=NOW,
            workload={},
            policy={},
            checks=[
                PolicyCheck(
                    gate="active_plan",
                    state=PolicyCheckState.REJECTED,
                    message="Another plan for this workload is still active.",
                    code="workload_plan_already_exists",
                )
            ],
            rejections=[
                PolicyRejection(code=AutomationErrorCode.WORKLOAD_PLAN_ALREADY_EXISTS, detail="Resolve the plan.")
            ],
        )
        decision = assess_eligibility(_record(), _ctx(policy_results={7: result}))
        assert decision["can_open_pr"] is True
        assert [w["code"] for w in decision["warnings"]] == ["workload_plan_already_exists"]

    async def test_evaluator_skips_records_without_a_safe_action(self):
        plans = MagicMock()
        plans.list_plan_records_for_workload = AsyncMock(return_value=[])
        assert await evaluate_policies([_record(action=None)], _ctx(), plans) == {}
        plans.list_plan_records_for_workload.assert_not_called()

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

    async def test_failed_reads_fail_closed(self, monkeypatch):
        """A failed policy evaluation or pull request read never yields can_open_pr: true."""
        monkeypatch.setattr(
            "greenkube.core.optimization.eligibility.AutomationPolicyEvaluator.evaluate",
            AsyncMock(side_effect=RuntimeError("database down")),
        )
        plans = MagicMock()
        ctx = _ctx(bindings={KEY: _binding()})
        results = await evaluate_policies([_record()], ctx, plans)
        assert 7 in results and results[7] is None
        decision = assess_eligibility(_record(), replace(ctx, policy_results=results))
        assert decision["can_open_pr"] is False
        assert "policy_evaluation_unavailable" in _codes(decision)
        assert "direct" not in decision["delivery_modes"]

        prs = MagicMock()
        prs.get_open_pull_requests = AsyncMock(side_effect=RuntimeError("database down"))
        open_prs = await load_open_pr_ids(prs)
        assert open_prs is None
        decision = assess_eligibility(_record(), _ctx(bindings={KEY: _binding()}, open_pr_recommendation_ids=open_prs))
        assert decision["can_open_pr"] is False
        assert "pull_requests_unavailable" in _codes(decision)

    async def test_failed_reads_do_not_block_records_that_cannot_open_a_pr_anyway(self):
        decision = assess_eligibility(
            _record(type=RecommendationType.ZOMBIE_POD, action=None),
            _ctx(bindings={KEY: _binding()}, open_pr_recommendation_ids=None),
        )
        assert "pull_requests_unavailable" not in _codes(decision)

    def test_criticality_from_binding_labels(self):
        ctx = _ctx(bindings={KEY: _binding(workload_labels={"greenkube.io/criticality": "critical"})})
        assert criticality_by_record([_record()], ctx) == {7: "critical"}


class TestContainerResourcesV2Actions:
    """H1: increases, limits and sidecars validate like decreases; unsafe edits stay blocked."""

    @staticmethod
    def _with(operation: SetContainerResources):
        action = _action().model_copy(update={"operations": [operation]})
        return _record(action=action, recommended_cpu_request_millicores=1500)

    def test_an_increase_with_limit_edits_is_a_valid_action(self):
        for operation in (
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "1500m"}, limits={"cpu": "2"})
            ),
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "1500m"}), remove_limits=["cpu"]
            ),
            SetContainerResources(
                container="api", resources=ContainerResources(requests={"cpu": "1500m"}), container_kind="sidecar"
            ),
        ):
            assert validate_action(self._with(operation)) == ([7], None)

    def test_a_memory_limit_removal_is_not_a_valid_action(self):
        record = self._with(SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "1"})))
        assert record.action is not None
        payload = record.action.model_dump(mode="json")
        payload["operations"][0]["remove_limits"] = ["memory"]
        assert validate_action(SimpleNamespace(**{**record.__dict__, "action": payload})) == (
            None,
            "requires_container_target",
        )

    def test_an_increase_is_not_a_no_op(self):
        operation = SetContainerResources(container="api", resources=ContainerResources(requests={"cpu": "1500m"}))
        assert not proposes_no_change(self._with(operation))


class TestWarmUpSinceInstall:
    """F7: minCoverage is evaluated against the data available since install, capped at the lookback."""

    def test_a_day_old_install_with_full_collection_is_not_blocked_by_the_week_lookback(self):
        # One day of 5-minute samples in a 7-day window: 1/7 coverage of the window,
        # but every step since install was collected.
        evidence = RecommendationEvidence(
            sample_count=288,
            coverage_ratio=288 / 2016,
            observation_window_start=NOW - timedelta(days=7),
            observation_window_end=NOW,
            observation_window_seconds=7 * 86400,
        )
        result = assess_eligibility(
            _record(evidence=evidence), _ctx(step_seconds=300)
        )  # no install time known: the legacy full-window coverage blocks
        assert _codes(result) == ["warming_up"]

        from greenkube.automation.policy import set_history_start

        set_history_start(NOW - timedelta(days=1))
        result = assess_eligibility(_record(evidence=evidence), _ctx(step_seconds=300))
        assert "warming_up" not in _codes(result)
        assert result["eligible_at"] is None

    def test_eligible_at_is_first_sample_plus_the_required_history(self):
        from greenkube.core.optimization.eligibility import warm_up

        install = NOW - timedelta(hours=1)
        evidence = RecommendationEvidence(
            sample_count=12,
            coverage_ratio=12 / 2016,
            observation_window_start=NOW - timedelta(days=7),
            observation_window_end=NOW,
            observation_window_seconds=7 * 86400,
        )
        blocker, eligible_at = warm_up(
            _record(evidence=evidence), AutomationPolicyConfig(), 300, NOW, history_start=install
        )
        assert blocker["code"] == "warming_up"
        assert eligible_at == install + timedelta(seconds=36 * 300)
        assert "Eligible in ~2h" in blocker["fix"]

    def test_a_workload_younger_than_the_install_is_eligible_once_its_own_samples_accrue(self):
        # e2e wave 4: a Deployment created a day after install, 1 of 6 samples, was
        # reported warming up with an eligible_at a day in the past.
        from greenkube.core.optimization.eligibility import warm_up

        install = NOW - timedelta(days=1)
        evidence = RecommendationEvidence(
            sample_count=1,
            coverage_ratio=1 / 2880,
            observation_window_start=install,
            observation_window_end=NOW,
            observation_window_seconds=86400,
        )
        blocker, eligible_at = warm_up(
            _record(evidence=evidence),
            AutomationPolicyConfig(min_samples=6, min_coverage=0.0),
            30,
            NOW,
            history_start=install,
        )
        assert blocker["code"] == "warming_up"
        assert eligible_at == NOW + timedelta(seconds=5 * 30)
        assert "Enough data should now be available" not in blocker["fix"]

    def test_the_lookback_caps_the_history_considered(self):
        from greenkube.automation.policy import coverage_since_history

        evidence = RecommendationEvidence(
            sample_count=1000,
            coverage_ratio=0.2,
            observation_window_start=NOW - timedelta(days=7),
            observation_window_end=NOW,
            observation_window_seconds=7 * 86400,
        )
        # Installed a month ago: the whole lookback is available, nothing changes.
        assert coverage_since_history(evidence, NOW - timedelta(days=30)) == pytest.approx(0.2)
        # Installed two days ago: 0.2 of the week is 1.4 days of the 2 available.
        assert coverage_since_history(evidence, NOW - timedelta(days=2)) == pytest.approx(0.7)

    def test_a_history_start_after_the_evidence_window_is_treated_as_unknown(self):
        """Stale evidence, or retention moving the first stored sample forward, must not inflate coverage."""
        from greenkube.automation.policy import coverage_since_history

        evidence = RecommendationEvidence(
            sample_count=200,
            coverage_ratio=0.1,
            observation_window_start=NOW - timedelta(days=7),
            observation_window_end=NOW - timedelta(days=1),
            observation_window_seconds=6 * 86400,
        )
        assert coverage_since_history(evidence, NOW - timedelta(hours=1)) == pytest.approx(0.1)


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``NOW`` for each test: a wall-clock constant taken at import ages during a long suite."""
    monkeypatch.setitem(globals(), "NOW", datetime.now(timezone.utc))


def test_action_unavailable_wording_covers_increases_and_decreases():
    """Two-way rightsizing: the fix must not say only a decrease can rebuild the action (K4)."""
    from greenkube.core.optimization.eligibility import CODES

    message, fix = CODES["action_unavailable"]

    assert "request decrease are" not in fix
    assert "increase or decrease" in fix
