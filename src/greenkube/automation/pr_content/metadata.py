"""Labels, draft state and reviewers derived from a plan (roadmap J2, J3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from greenkube.automation.pr_content.model import PullRequestContext, highest_risk, resource_changes
from greenkube.gitops.contracts import AddHorizontalPodAutoscaler, DeleteResource, SetContainerResources

BASE_LABEL = "greenkube"
RELIABILITY_LABEL = "reliability"
_ACTION_CAPABILITIES = {
    SetContainerResources: "rightsizing",
    DeleteResource: "cleanup",
    AddHorizontalPodAutoscaler: "autoscaling",
}


@dataclass(frozen=True, slots=True)
class PullRequestMetadata:
    """Metadata GreenKube derives for one plan PR, on top of the configured options."""

    labels: tuple[str, ...] = ()
    draft: bool = False
    risk_level: str | None = None
    reviewers: tuple[str, ...] = ()
    team_reviewers: tuple[str, ...] = ()


def derive_metadata(
    context: PullRequestContext,
    *,
    default_labels: bool = True,
    draft_high_risk: bool = True,
) -> PullRequestMetadata:
    """``greenkube``, ``optimization/<capability>``, ``risk/<level>`` and ``reliability`` labels.

    A plan is a draft when its riskiest recommendation is ``high``.
    """
    risk = highest_risk(context.recommendations)
    labels: list[str] = []
    if default_labels:
        labels.append(BASE_LABEL)
        for capability in plan_capabilities(context):
            _append(labels, f"optimization/{capability}")
        if risk is not None:
            _append(labels, f"risk/{risk}")
        if is_reliability_change(context):
            _append(labels, RELIABILITY_LABEL)
    return PullRequestMetadata(
        labels=tuple(labels),
        draft=bool(draft_high_risk and risk == "high"),
        risk_level=risk,
    )


def plan_capabilities(context: PullRequestContext) -> list[str]:
    """Capabilities of the plan's recommendations, or of its action kinds when none are loaded."""
    capabilities: list[str] = []
    for record in context.recommendations:
        capability = getattr(record.capability, "value", record.capability)
        if isinstance(capability, str) and capability:
            _append(capabilities, capability.replace("_", "-"))
    if not capabilities:
        for operation in context.plan.action.operations:
            capability = _ACTION_CAPABILITIES.get(type(operation))
            if capability is not None:
                _append(capabilities, capability)
    return capabilities


def is_reliability_change(context: PullRequestContext) -> bool:
    """Whether the plan raises a resource (an increase made for reliability, not savings)."""
    if context.is_rollback:
        return False
    if any(change.increases for change in resource_changes(context)):
        return True
    return any(
        isinstance(record.value, dict) and record.value.get("lane") == "reliability"
        for record in context.recommendations
    )


def merge_unique(*groups: Sequence[str]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            _append(merged, item)
    return tuple(merged)


def _append(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
