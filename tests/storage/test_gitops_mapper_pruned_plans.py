"""Regression tests for reading plans whose file contents were pruned (roadmap D2, BUG-44)."""

import json
from datetime import datetime, timezone

import pytest

from greenkube.automation.policy import AutomationPolicyConfig, AutomationPolicyEvaluator
from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    FilePlanSummary,
    Plan,
    PlanStatus,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.storage.gitops_mapper import pruned_file_plan_summary_payload, row_to_automation_plan_record

_DIGEST = "sha256:" + "a" * 64
_OUTPUT_DIGEST = "sha256:" + "b" * 64
_VALIDATION = {
    "target_changed": True,
    "unrelated_changes": False,
    "requests_within_limits": True,
    "renderer": "raw",
    "output_digest": _OUTPUT_DIGEST,
}


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="s",
        cluster="c",
        namespace="ns",
        workload_kind="Deployment",
        workload_name="w",
        provider_id="p",
        repo_url="https://github.com/a/b",
        ref="main",
        path=".",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="app",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=1,
        status=BindingStatus.RESOLVED,
    )


def _action() -> ActionPlanV3:
    return ActionPlanV3(
        action_id="a",
        recommendation_ids=[1],
        target=ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="ns", name="w"),
        operations=[SetContainerResources(container="c", resources=ContainerResources(requests={"cpu": "100m"}))],
        evidence_ref="x",
    )


def _row(status: str = "applied", **overrides) -> dict:
    row = {
        "source_snapshot": _source().model_dump_json(),
        "action_json": _action().model_dump_json(),
        "policy_result": "{}",
        "digest": _DIGEST,
        "base_revision": "a" * 40,
        "renderer": "raw",
        "validation": json.dumps(_VALIDATION),
        "diff": "--- a/deploy.yaml\n+++ b/deploy.yaml\n-  cpu: 200m\n+  cpu: 100m\n",
        "plan_id": "plan-1",
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("status", ["applied", "sync_failed", "synced", "pr_open"])
def test_pruned_validated_plan_rows_map_to_a_valid_plan(status):
    """Port of prune_check.py: the mapper raised ValidationError once plan files were deleted."""
    record = row_to_automation_plan_record(_row(status), file_rows=[], recommendation_ids=[1])

    plan = record.plan
    assert plan.status is PlanStatus(status)
    assert plan.file_plan is None
    assert plan.file_plan_summary is not None
    assert plan.file_plan_summary.files == []
    assert plan.file_plan_summary.digest == _DIGEST
    assert plan.file_plan_summary.diff_stats is not None
    assert plan.file_plan_summary.diff_stats.additions == 1
    assert plan.file_plan_summary.diff_stats.deletions == 1
    assert plan.base_revision == "a" * 40


def test_stored_summary_is_preferred_over_a_rebuilt_one():
    file_rows = [{"path": "deploy.yaml", "operation": "update", "original_sha": "c" * 40, "content": "cpu: 100m\n"}]
    pruned_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    payload = pruned_file_plan_summary_payload(_row(), file_rows, pruned_at=pruned_at)
    assert payload is not None

    record = row_to_automation_plan_record(
        _row(file_plan_summary=json.dumps(payload)),
        file_rows=[],
        recommendation_ids=[1],
    )

    summary = record.plan.file_plan_summary
    assert summary is not None
    assert [entry.path for entry in summary.files] == ["deploy.yaml"]
    assert summary.files[0].original_sha == "c" * 40
    assert summary.files[0].size_bytes == len("cpu: 100m\n")
    assert summary.pruned_at == pruned_at


def test_summary_for_another_digest_is_ignored():
    other = FilePlanSummary(
        plan_id="plan-1",
        base_revision="a" * 40,
        renderer=RendererKind.RAW,
        validation=ValidationResult.model_validate(_VALIDATION),
        digest="sha256:" + "f" * 64,
    )
    record = row_to_automation_plan_record(
        _row(file_plan_summary=other.model_dump_json()),
        file_rows=[],
        recommendation_ids=[1],
    )
    assert record.plan.file_plan_summary is not None
    assert record.plan.file_plan_summary.digest == _DIGEST


def test_plan_validator_accepts_a_summary_only_for_matching_validated_content():
    summary = FilePlanSummary(
        plan_id="plan-1",
        base_revision="a" * 40,
        renderer=RendererKind.RAW,
        validation=ValidationResult.model_validate(_VALIDATION),
        digest=_DIGEST,
    )
    plan = Plan(
        plan_id="plan-1",
        recommendation_ids=[1],
        source=_source(),
        action=_action(),
        file_plan_summary=summary,
        status=PlanStatus.APPLIED,
        digest=_DIGEST,
    )
    assert plan.file_summary == summary

    with pytest.raises(ValueError, match="digest"):
        Plan(
            plan_id="plan-1",
            recommendation_ids=[1],
            source=_source(),
            action=_action(),
            file_plan_summary=summary,
            status=PlanStatus.APPLIED,
            digest="sha256:" + "e" * 64,
        )
    with pytest.raises(ValueError):
        Plan(
            plan_id="plan-1",
            recommendation_ids=[1],
            source=_source(),
            action=_action(),
            status=PlanStatus.APPLIED,
            digest=_DIGEST,
        )


def test_file_summary_is_derived_from_a_live_file_plan():
    file_plan = FilePlan(
        plan_id="plan-1",
        files=[FileEdit(path="deploy.yaml", operation="update", original_sha="c" * 40, content="x\n")],
        diff="+++ b/deploy.yaml\n+x\n",
        rendered_diff="",
        base_revision="a" * 40,
        renderer=RendererKind.RAW,
        validation=ValidationResult.model_validate(_VALIDATION),
        digest=_DIGEST,
    )
    plan = Plan(
        plan_id="plan-1",
        recommendation_ids=[1],
        source=_source(),
        action=_action(),
        file_plan=file_plan,
        status=PlanStatus.VALIDATED,
        digest=_DIGEST,
    )
    summary = plan.file_summary
    assert summary is not None
    assert summary.files[0].content_digest is not None
    assert summary.diff_stats is not None and summary.diff_stats.additions == 1


@pytest.mark.asyncio
async def test_policy_history_reads_tolerate_pruned_plans():
    """New plans for a workload whose old plans were pruned must not get POLICY_EVIDENCE_MISSING."""
    pruned = row_to_automation_plan_record(_row("applied"), file_rows=[], recommendation_ids=[1]).plan

    class _Repository:
        async def list_plans_for_workload(self, **kwargs):
            return [pruned]

        async def list_plans(self, **kwargs):
            return [pruned]

        async def get_plan_record(self, plan_id):
            return row_to_automation_plan_record(
                _row("applied", updated_at="2025-01-01T00:00:00+00:00"),
                file_rows=[],
                recommendation_ids=[1],
            )

    evaluator = AutomationPolicyEvaluator(config=AutomationPolicyConfig(), plan_repository=_Repository())
    result = await evaluator.evaluate([], _source())
    history_gates = {
        check.gate: check
        for check in result.checks
        if check.gate in {"active_plan", "cooldown", "open_pull_request_budget"}
    }
    assert set(history_gates) == {"active_plan", "cooldown", "open_pull_request_budget"}
    assert all(check.state.value == "passed" for check in history_gates.values())
    assert not any(
        rejection.code is AutomationErrorCode.POLICY_EVIDENCE_MISSING and "history" in rejection.detail
        for rejection in result.rejections
    )
