"""An unknown OOM history is a warning a person may override, never an automatic PR (2026-11-15).

A memory decrease whose OOM-kill and restart history could not be read carries
the risk factor ``oom_history_unknown`` (and ``oom_unknown_<cause>``). A person
may open the pull request, with a warning naming the cause and what to check
before merging; policy-based auto-approval never approves it, and the pull
request body repeats the warning for the reviewer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from greenkube.automation.pr_content import PullRequestContext, PullRequestRenderSettings, render_body
from greenkube.core.optimization.eligibility import (
    AUTO_APPROVE_CODES,
    AutoApprovePolicy,
    EligibilityContext,
    assess_eligibility,
    auto_approve_blockers,
)
from greenkube.core.optimization.risks import oom_history_unknown_warning
from greenkube.models.metrics import RecommendationType
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
CAUSES = {
    "no_usage_summary": "No usage summary covers this container",
    "signal_failed": "queries to Prometheus failed",
    "signal_unavailable": "kube-state-metrics not scraped",
}


def _record(factors, **overrides):
    values = dict(
        id=1,
        type="RIGHTSIZING_MEMORY",
        status="active",
        risk_level="medium",
        risk_factors=factors,
        confidence=0.95,
        current_memory_request_bytes=2048,
        recommended_memory_request_bytes=1024,
        source="greenkube",
        evidence=NS(sample_count=288),
        value=None,
        consensus=None,
        eligibility=None,
    )
    values.update(overrides)
    return NS(**values)


@pytest.mark.parametrize(("cause", "fragment"), sorted(CAUSES.items()))
def test_unknown_oom_history_is_a_warning_naming_the_cause_and_what_to_check(cause, fragment):
    result = assess_eligibility(_record(["oom_history_unknown", f"oom_unknown_{cause}"]), EligibilityContext(now=NOW))
    assert "oom_history_unknown" not in {entry["code"] for entry in result["blockers"]}
    [warning] = [entry for entry in result["warnings"] if entry["code"] == "oom_history_unknown"]
    assert fragment in warning["message"]
    assert "Before merging" in warning["fix"]
    assert "restarts" in warning["fix"] and "OOMKilled" in warning["fix"]


def test_unknown_oom_history_alone_does_not_block_a_human_pull_request():
    ctx = EligibilityContext(now=NOW, automation_configured=True, automation_enabled=True, bindings={})
    blockers_with = {entry["code"] for entry in assess_eligibility(_record(["oom_history_unknown"]), ctx)["blockers"]}
    blockers_without = {entry["code"] for entry in assess_eligibility(_record([]), ctx)["blockers"]}
    assert blockers_with == blockers_without


def test_auto_approval_never_approves_an_unknown_oom_history():
    policy = AutoApprovePolicy(enabled=True, max_risk="medium", min_confidence=0.8)
    assert auto_approve_blockers([_record([])], policy, expected=1) == []
    blocked = auto_approve_blockers([_record(["oom_history_unknown", "oom_unknown_signal_failed"])], policy, expected=1)
    assert blocked == ["oom_history_unknown"]
    assert "oom_history_unknown" in AUTO_APPROVE_CODES
    # The eligibility warning alone (risk factors not loaded) also stops it: fail closed.
    eligibility = {"warnings": [{"code": "oom_history_unknown", "message": "", "fix": ""}], "blockers": []}
    assert auto_approve_blockers([_record([], eligibility=eligibility)], policy, expected=1) == ["oom_history_unknown"]


def test_the_warning_helper_is_none_without_the_factor():
    assert oom_history_unknown_warning(["oom_history"]) is None
    texts = oom_history_unknown_warning(["oom_history_unknown"])
    assert texts is not None
    message, check = texts
    assert "could not be read" in message and "Before merging" in check


@pytest.mark.parametrize(("cause", "fragment"), sorted(CAUSES.items()))
def test_the_pull_request_body_carries_the_warning(cause, fragment):
    record = fx.recommendation(
        42,
        rec_type=RecommendationType.RIGHTSIZING_MEMORY,
        risk_level="medium",
        risk_factors=["oom_history_unknown", f"oom_unknown_{cause}"],
    )
    context = PullRequestContext(
        plan=fx.rightsize_plan(),
        repository="acme/gitops",
        branch="greenkube/plan/0123456789abcdef0123456789abcdef",
        recommendations=(record,),
    )
    body = render_body(context, PullRequestRenderSettings())
    summary = body.split("## Changes")[0]
    assert "OOM history unknown" in summary
    assert fragment in summary
    assert "Before merging" in summary and "OOMKilled" in summary


def test_the_pull_request_body_has_no_warning_without_the_factor():
    context = PullRequestContext(
        plan=fx.rightsize_plan(),
        repository="acme/gitops",
        branch="greenkube/plan/0123456789abcdef0123456789abcdef",
        recommendations=(fx.recommendation(42, rec_type=RecommendationType.RIGHTSIZING_MEMORY),),
    )
    assert "OOM history unknown" not in render_body(context, PullRequestRenderSettings())
