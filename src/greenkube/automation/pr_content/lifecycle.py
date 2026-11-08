"""The single GreenKube status comment kept up to date on a plan PR (roadmap J5).

The comment is a timeline: merged, synced (revision, controller), applied
(rollout completed), measuring (ETA) and the outcome (realized savings with
their confidence interval, not realized, inconclusive, contaminated or
rollback review), with a link to the ledger. It starts with
:data:`LIFECYCLE_MARKER` so the publisher can find and edit it instead of
posting a new one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from greenkube.automation.pr_content import formatting as fmt
from greenkube.automation.pr_content.model import PullRequestRenderSettings

LIFECYCLE_MARKER = "<!-- greenkube:lifecycle v1 -->"
StepState = Literal["done", "current", "failed", "waiting"]
_ICONS = {"done": "✅", "current": "⏳", "failed": "❌", "waiting": "⬜"}
_OUTCOME_TEXT = {
    "realized": "Realized",
    "not_realized": "Not realized",
    "inconclusive": "Inconclusive",
    "contaminated": "Contaminated",
    "regressed": "Regressed after being realized",
}


@dataclass(frozen=True, slots=True)
class MeasuredQuantity:
    """A measured annual saving with its 90% confidence interval (positive is a saving)."""

    annualized: float | None
    ci_low: float | None = None
    ci_high: float | None = None


@dataclass(frozen=True, slots=True)
class LifecycleState:
    """What GreenKube knows about one plan after its PR was opened."""

    plan_id: str
    pr_status: str = "open"
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    sync_status: str | None = None
    sync_revision: str | None = None
    sync_observed_at: datetime | None = None
    sync_error_code: str | None = None
    controller: str = ""
    applied_at: datetime | None = None
    change_id: str | None = None
    measurement_state: str | None = None
    measurement_ends_at: datetime | None = None
    outcome: str | None = None
    outcome_reasons: tuple[str, ...] = ()
    cost: MeasuredQuantity | None = None
    co2e_g: MeasuredQuantity | None = None
    currency: str = "USD"
    rollback_review: bool = False
    rollback_reason: str | None = None
    newer_advice: str | None = None

    @property
    def closed_unmerged(self) -> bool:
        return self.pr_status == "closed" and self.merged_at is None and self.merge_commit_sha is None


def render_lifecycle_comment(state: LifecycleState, settings: PullRequestRenderSettings | None = None) -> str:
    selected = settings or PullRequestRenderSettings()
    rows = [
        ("Merged", *_merged(state)),
        ("Synced", *_synced(state)),
        ("Applied", *_applied(state)),
        ("Measuring", *_measuring(state)),
        ("Outcome", *_outcome(state)),
    ]
    lines = [
        LIFECYCLE_MARKER,
        "### GreenKube status",
        "",
        "| Step | | Details |",
        "| --- | :---: | --- |",
        *(
            f"| {name} | {_ICONS[step_state]} | {fmt.table_cell(detail, max_chars=400)} |"
            for name, step_state, detail in rows
        ),
    ]
    summary = _outcome_summary(state)
    if summary:
        lines.extend(("", summary))
    if state.newer_advice:
        lines.extend(("", f"> **Newer advice available:** {fmt.inline(state.newer_advice, max_chars=500)}"))
    links = []
    change_link = selected.link(f"changes/{state.plan_id}")
    if change_link:
        links.append(f"[Change]({change_link})")
    ledger_link = selected.link(f"ledger?change={state.change_id or state.plan_id}")
    if ledger_link:
        links.append(f"[Savings ledger]({ledger_link})")
    footer = f"_Plan `{state.plan_id}`. Savings in the PR description are projections; values here are measured._"
    lines.extend(("", footer + (" " + " · ".join(links) if links else "")))
    return "\n".join(lines) + "\n"


def comment_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _merged(state: LifecycleState) -> tuple[StepState, str]:
    if state.closed_unmerged:
        return "failed", "Closed without merging."
    if state.merged_at is None and state.merge_commit_sha is None:
        return "current", "Waiting for review and merge."
    detail = f"Merge commit `{fmt.short_sha(state.merge_commit_sha)}`"
    if state.merged_at is not None:
        detail += f" at {fmt.timestamp(state.merged_at)}"
    return "done", detail + "."


def _synced(state: LifecycleState) -> tuple[StepState, str]:
    if state.sync_status == "synced":
        detail = f"Revision `{fmt.short_sha(state.sync_revision)}`"
        if state.controller:
            detail += f" by {state.controller}"
        if state.sync_observed_at is not None:
            detail += f", observed {fmt.timestamp(state.sync_observed_at)}"
        return "done", detail + "."
    if state.sync_status == "failed":
        code = state.sync_error_code or "sync_failed"
        if code == "sync_timeout":
            return "failed", "The controller has not synced the merge within the timeout; GreenKube keeps retrying."
        return "failed", f"Sync did not complete (`{code}`)."
    if state.applied_at is not None:
        return "done", "Confirmed by the completed rollout."
    if state.merged_at is None and state.merge_commit_sha is None:
        return "waiting", ""
    reason = f" (`{state.sync_error_code}`)" if state.sync_error_code else ""
    return "current", f"Waiting for {state.controller or 'the GitOps controller'} to sync the merge{reason}."


def _applied(state: LifecycleState) -> tuple[StepState, str]:
    if state.applied_at is not None:
        return "done", f"Rollout completed at {fmt.timestamp(state.applied_at)}."
    if state.sync_status == "synced":
        return "current", "Waiting for the rollout to complete."
    return "waiting", ""


def _measuring(state: LifecycleState) -> tuple[StepState, str]:
    if state.outcome is not None:
        return "done", "Measurement complete."
    if state.measurement_state in {"measuring", "applied"} or (state.applied_at is not None and state.outcome is None):
        eta = (
            f" Results expected around {fmt.timestamp(state.measurement_ends_at)}." if state.measurement_ends_at else ""
        )
        return "current", f"Measuring savings.{eta}"
    return "waiting", ""


def _outcome(state: LifecycleState) -> tuple[StepState, str]:
    if state.rollback_review:
        reason = f": {state.rollback_reason}" if state.rollback_reason else ""
        return "failed", f"Rollback review{reason}"
    if state.outcome is None:
        return "waiting", ""
    text = _OUTCOME_TEXT.get(state.outcome, state.outcome)
    return ("done" if state.outcome == "realized" else "failed"), text + "."


def _outcome_summary(state: LifecycleState) -> str:
    if state.rollback_review:
        return (
            "**Rollback review:** a health gate failed after the rollout. Review the change and, if needed, open "
            "the GreenKube rollback pull request."
        )
    if state.outcome is None:
        return ""
    parts = []
    if state.cost is not None and state.cost.annualized is not None:
        parts.append(f"cost {_quantity(state.cost, lambda value: fmt.money(value, state.currency))}")
    if state.co2e_g is not None and state.co2e_g.annualized is not None:
        parts.append(f"CO₂e {_quantity(state.co2e_g, fmt.co2e)} (reported)")
    label = _OUTCOME_TEXT.get(state.outcome, state.outcome)
    text = f"**Outcome: {label}.**"
    if parts:
        text += " Measured saving: " + "; ".join(parts) + "."
    if state.outcome_reasons:
        text += " " + "; ".join(fmt.inline(reason, max_chars=200) for reason in state.outcome_reasons[:3]) + "."
    return text


def _quantity(quantity: MeasuredQuantity, render) -> str:
    assert quantity.annualized is not None
    text = f"{render(quantity.annualized)}/yr"
    if quantity.ci_low is not None and quantity.ci_high is not None:
        text += f" (90% CI {render(quantity.ci_low)} to {render(quantity.ci_high)})"
    return text
