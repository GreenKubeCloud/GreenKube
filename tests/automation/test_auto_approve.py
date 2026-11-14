"""Policy-based auto-approval (roadmap F5): eligible previews skip the approval click.

Contested consensus is a hard stop, and an auto-approved decrease needs GreenKube's
own usage evidence or at least two independent consensus votes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from greenkube.automation.plans import AutomationPlanError, maybe_auto_approve
from greenkube.core.config import Config
from greenkube.core.optimization.eligibility import (
    AUTO_APPROVE_CODES,
    AutoApprovePolicy,
    auto_approve_blockers,
    has_native_evidence,
    is_contested,
    proposes_decrease,
    proposes_increase,
)
from greenkube.gitops.contracts import AutomationErrorCode, PlanStatus

POLICY = AutoApprovePolicy(enabled=True, max_risk="low", min_confidence=0.8)


def _record(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 42,
        "type": "RIGHTSIZING_CPU",
        "status": "active",
        "risk_level": "low",
        "confidence": 0.9,
        "current_cpu_request_millicores": 1000,
        "recommended_cpu_request_millicores": 400,
        "source": "greenkube",
        "evidence": SimpleNamespace(sample_count=288),
        "consensus": None,
        "eligibility": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_policy_reads_the_settings_and_is_off_by_default():
    assert AutoApprovePolicy.from_config(Config()) == AutoApprovePolicy(
        enabled=False, max_risk="low", min_confidence=0.8
    )
    configured = Config(
        AUTOMATION_AUTO_APPROVE_ENABLED=True,
        AUTOMATION_AUTO_APPROVE_MAX_RISK="Medium",
        AUTOMATION_AUTO_APPROVE_MIN_CONFIDENCE=0.7,
    )
    assert AutoApprovePolicy.from_config(configured) == AutoApprovePolicy(True, "medium", 0.7)
    with pytest.raises(ValueError):
        Config(AUTOMATION_AUTO_APPROVE_CONTESTED=True)
    with pytest.raises(ValueError):
        Config(AUTOMATION_AUTO_APPROVE_MAX_RISK="none")
    assert auto_approve_blockers([_record()], AutoApprovePolicy(), expected=1) == ["auto_approve_disabled"]


def test_a_low_risk_confident_native_decrease_is_approved_automatically():
    assert auto_approve_blockers([_record()], POLICY, expected=1) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"risk_level": "medium"}, "risk_above_auto_approve"),
        ({"risk_level": None}, "risk_above_auto_approve"),
        ({"confidence": 0.79}, "confidence_below_auto_approve"),
        ({"confidence": None}, "confidence_below_auto_approve"),
        ({"status": "ignored"}, "not_active"),
        ({"consensus": {"contested": True, "independent_votes": 3}}, "consensus_contested"),
        (
            {"eligibility": {"warnings": [{"code": "consensus_contested", "message": "", "fix": ""}]}},
            "consensus_contested",
        ),
        (
            {"source": "vpa", "consensus": {"chosen_from": ["vpa"], "independent_votes": 1}},
            "decrease_without_native_evidence",
        ),
        ({"evidence": None}, "decrease_without_native_evidence"),
    ],
    ids=[
        "risk",
        "no-risk",
        "confidence",
        "no-confidence",
        "inactive",
        "contested",
        "contested-warning",
        "vpa-only",
        "no-evidence",
    ],
)
def test_each_rule_keeps_the_preview_for_a_human(overrides, code):
    blockers = auto_approve_blockers([_record(**overrides)], POLICY, expected=1)

    assert code in blockers
    assert code in AUTO_APPROVE_CODES


def test_contested_consensus_is_a_hard_stop_even_with_many_agreeing_votes():
    contested = _record(consensus={"contested": True, "independent_votes": 5, "chosen_from": ["greenkube.cpu"]})

    assert is_contested(contested)
    assert auto_approve_blockers([contested], AutoApprovePolicy(True, "high", 0.0), expected=1) == [
        "consensus_contested"
    ]


def test_pushed_votes_of_a_stored_consensus_never_count_as_two_independent_votes():
    """A record scored before pushed votes were merged still says independent_votes=2 (security hardening)."""

    def vote(source_id: str, *, pushed: bool | None = None, counted: bool = True) -> dict[str, Any]:
        entry: dict[str, Any] = {"source_id": source_id, "family": source_id, "counted": counted}
        if pushed is not None:
            entry["pushed"] = pushed
        return entry

    stale = _record(
        source="push:a",
        evidence=None,
        consensus={"chosen_from": ["push:a"], "independent_votes": 2, "votes": [vote("push:a"), vote("push:b")]},
    )
    report_and_client = _record(
        source="krr",
        evidence=None,
        consensus={"chosen_from": ["krr"], "independent_votes": 2, "votes": [vote("krr"), vote("push:ci")]},
    )
    flagged = _record(
        source="custom",
        evidence=None,
        consensus={
            "chosen_from": ["custom"],
            "independent_votes": 2,
            "votes": [vote("custom", pushed=True), vote("other", pushed=True)],
        },
    )
    corroborated = _record(
        source="krr",
        evidence=None,
        consensus={"chosen_from": ["krr", "vpa"], "independent_votes": 2, "votes": [vote("krr"), vote("vpa")]},
    )

    for record in (stale, report_and_client, flagged):
        assert auto_approve_blockers([record], POLICY, expected=1) == ["decrease_without_native_evidence"]
    assert auto_approve_blockers([corroborated], POLICY, expected=1) == []


def test_a_decrease_from_external_sources_needs_two_independent_votes():
    one_vote = _record(source="krr", evidence=None, consensus={"chosen_from": ["krr"], "independent_votes": 1})
    two_votes = _record(source="krr", evidence=None, consensus={"chosen_from": ["krr", "vpa"], "independent_votes": 2})
    increase = _record(
        source="krr",
        evidence=None,
        current_cpu_request_millicores=200,
        recommended_cpu_request_millicores=400,
    )

    assert auto_approve_blockers([one_vote], POLICY, expected=1) == ["decrease_without_native_evidence"]
    assert auto_approve_blockers([two_votes], POLICY, expected=1) == []
    assert not proposes_decrease(increase)
    # An increase sized by another source may exceed the wave-3a bounds (H1): never approved automatically.
    assert auto_approve_blockers([increase], AutoApprovePolicy(True, "low", 0.8, increases=True), expected=1) == [
        "increase_without_native_evidence"
    ]
    assert has_native_evidence(_record(source="krr", consensus={"chosen_from": ["greenkube.rightsizing_cpu"]}))


def test_a_recommendation_that_could_not_be_read_blocks():
    assert auto_approve_blockers([_record()], POLICY, expected=2) == ["recommendation_missing"]


class _Approver:
    def __init__(self, error: AutomationPlanError | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.error = error
        self.plan: Any = None

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system"):
        self.calls.append((plan_id, digest, actor))
        if self.error is not None:
            raise self.error
        return self.plan.model_copy(update={"status": PlanStatus.APPROVED})


def _validated_plan():
    from tests.automation.test_plan_worker import _plan  # pyrefly: ignore[missing-import]

    plan = _plan(status=PlanStatus.VALIDATED)
    action = plan.action.model_copy(update={"recommendation_ids": [42]})
    return plan.model_copy(update={"recommendation_ids": [42], "action": action})


@pytest.mark.asyncio
async def test_hook_approves_through_the_normal_path_on_behalf_of_the_requester():
    plan = _validated_plan()
    approver = _Approver()
    approver.plan = plan

    approved, blockers = await maybe_auto_approve(
        approver, plan, [_record()], actor="user:alice", policy=POLICY, may_approve=True
    )

    assert blockers == [] and approved.status is PlanStatus.APPROVED
    assert approver.calls == [(plan.plan_id, plan.digest, "autoapprove:user:alice")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"may_approve": False}, "approve_scope_missing"),
        ({"policy": AutoApprovePolicy()}, "auto_approve_disabled"),
        ({"records": [_record(consensus={"contested": True})]}, "consensus_contested"),
    ],
)
async def test_hook_leaves_the_preview_for_the_click(kwargs, code):
    plan = _validated_plan()
    approver = _Approver()
    approver.plan = plan
    options: dict[str, Any] = {"policy": POLICY, "may_approve": True, **kwargs}
    records = options.pop("records", [_record()])

    result, blockers = await maybe_auto_approve(approver, plan, records, actor="user:alice", **options)

    assert code in blockers and result is plan and approver.calls == []


@pytest.mark.asyncio
async def test_a_refused_approval_keeps_the_preview_and_explains_why():
    plan = _validated_plan()
    approver = _Approver(AutomationPlanError(AutomationErrorCode.DELIVERY_PAUSED, "paused"))
    approver.plan = plan

    result, blockers = await maybe_auto_approve(
        approver, plan, [_record()], actor="user:alice", policy=POLICY, may_approve=True
    )

    assert result is plan and blockers == ["delivery_paused"]
    rollback = plan.model_copy(update={"rollback_of_plan_id": "original"})
    assert (await maybe_auto_approve(approver, rollback, [_record()], actor="a", policy=POLICY, may_approve=True))[
        1
    ] == ["rollback_plan"]


@pytest.mark.asyncio
async def test_an_unexpected_approval_failure_leaves_the_preview_for_the_click():
    plan = _validated_plan()
    approver = _Approver()
    approver.plan = plan

    async def broken(plan_id: str, digest: str, *, actor: str = "system"):
        raise RuntimeError("revision resolver unavailable")

    approver.approve_plan = broken  # type: ignore[method-assign]

    result, blockers = await maybe_auto_approve(
        approver, plan, [_record()], actor="user:alice", policy=POLICY, may_approve=True
    )

    assert result is plan and blockers == ["auto_approve_failed"]


def test_an_increase_is_never_auto_approved_without_the_opt_in():
    """H1: a request increase is a reliability fix that costs money; a person approves it by default."""
    increase = _record(current_cpu_request_millicores=200, recommended_cpu_request_millicores=400)
    memory_increase = _record(
        type="RIGHTSIZING_MEMORY",
        current_memory_request_bytes=256 * 2**20,
        recommended_memory_request_bytes=512 * 2**20,
    )
    assert proposes_increase(increase) and proposes_increase(memory_increase)
    assert not proposes_increase(_record())
    for record in (increase, memory_increase):
        assert auto_approve_blockers([record], POLICY, expected=1) == ["increase_requires_approval"]
    assert "autoApprove.increases" in AUTO_APPROVE_CODES["increase_requires_approval"]
    opted_in = AutoApprovePolicy.from_config(
        Config(AUTOMATION_AUTO_APPROVE_ENABLED=True, AUTOMATION_AUTO_APPROVE_INCREASES=True)
    )
    assert opted_in.increases
    assert auto_approve_blockers([increase], opted_in, expected=1) == []
    assert not AutoApprovePolicy.from_config(Config()).increases
