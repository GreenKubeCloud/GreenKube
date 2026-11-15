"""Pull-request title, body and commit message for a GitOps plan (roadmap J1).

The body answers, in this order: what changes and what it saves (summary and
changes), why (consensus and evidence), what could go wrong (risk), which
numbers the projection rests on (assumptions), how to undo it (rollback) and
how GreenKube will prove the result (verification plan). Provenance and the
diffs follow in collapsible blocks, and a machine-readable ``greenkube:v1``
footer closes the body.

Every saving shown here is a projection; measured outcomes are posted later by
the lifecycle comment and are never added to these numbers (principle 3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from greenkube.automation.actions import operation_kind_of
from greenkube.automation.pr_content import formatting as fmt
from greenkube.automation.pr_content.model import (
    PullRequestContext,
    PullRequestRenderSettings,
    ResourceChange,
    capacity_totals,
    edits_limits,
    highest_risk,
    plan_verb,
    raises_requests,
    resource_changes,
    savings_totals,
)
from greenkube.automation.pr_content.template import PLACEHOLDER, render_template
from greenkube.gitops.contracts import (
    PLANNABLE_RENDERERS,
    AddHorizontalPodAutoscaler,
    ControllerKind,
    DeleteResource,
)
from greenkube.models.metrics import RecommendationRecord

FOOTER_PREFIX = "<!-- greenkube:v1 "
#: The footer is the body's last line. Diffs and recommendation text (repository
#: and user content) come before it and may contain a look-alike, so only a footer
#: that ends the body is trusted.
_FOOTER = re.compile(r"(?:\A|\n)<!-- greenkube:v1 (\{[^\n]*\}) -->\s*\Z")
#: Hard cap of each essential section, so sections 1-7 always fit in the body.
MAX_SECTION_CHARS = 6_000
_MIN_SECTION_CHARS = 200
#: Room kept for the truncation note so the hard cut never lands in a section.
_BODY_MARGIN = 120
_MIN_DIFF_CHARS = 400
_CONTROLLER_NAMES = {
    ControllerKind.ARGOCD: "Argo CD Application",
    ControllerKind.FLUX_KUSTOMIZATION: "Flux Kustomization",
    ControllerKind.FLUX_HELM_RELEASE: "Flux HelmRelease",
    ControllerKind.MANUAL_RESOLUTION: "manually resolved source",
}
_DEFAULT_WATCH = (
    "container restarts and `OOMKilled` events",
    "CPU throttling of the changed containers",
    "readiness, error rate and latency of the service",
)


@dataclass(frozen=True, slots=True)
class PullRequestContent:
    title: str
    body: str
    commit_message: str


def render_pull_request(
    context: PullRequestContext,
    settings: PullRequestRenderSettings | None = None,
) -> PullRequestContent:
    selected = settings or PullRequestRenderSettings()
    title = render_title(context, selected)
    return PullRequestContent(
        title=title,
        body=render_body(context, selected, title=title),
        commit_message=render_commit_message(context, selected, title=title),
    )


# --------------------------------------------------------------------------- #
# Title                                                                        #
# --------------------------------------------------------------------------- #


def render_title(context: PullRequestContext, settings: PullRequestRenderSettings | None = None) -> str:
    """``greenkube: rightsize payments/api (cpu 500m→200m, mem 1Gi→512Mi) · −$412/yr · −18 kgCO₂e/yr``.

    Parts are dropped from the right of the details when the title exceeds the
    limit (72 characters by default); the full summary is always in the body.
    """
    selected = settings or PullRequestRenderSettings()
    prefix = f"{selected.title_prefix}: " if selected.title_prefix else ""
    head = f"{prefix}{plan_verb(context)} {context.workload}"
    details = _title_details(context)
    savings = [] if context.is_rollback else _title_savings(context)
    limit = max(selected.max_title_chars, 20)
    candidates = []
    if details:
        candidates.append(_join_title(f"{head} ({details})", savings))
    candidates.append(_join_title(head, savings))
    candidates.append(_join_title(head, savings[:1]))
    candidates.append(head)
    for candidate in candidates:
        if len(candidate) <= limit:
            return candidate
    return candidates[-1][: limit - 1].rstrip() + "…"


def _join_title(head: str, savings: Sequence[str]) -> str:
    return " · ".join([head, *savings])


def _title_details(context: PullRequestContext) -> str:
    operations = context.plan.action.operations
    if context.is_rollback:
        return f"plan {context.plan.rollback_of_plan_id}"
    changes = [change for change in resource_changes(context) if change.field.startswith("requests.")]
    if changes:
        multiple = len({change.container for change in changes}) > 1
        parts = []
        for change in changes:
            label = "cpu" if change.is_cpu else "mem" if change.resource == "memory" else change.resource
            if multiple:
                label = f"{change.container} {label}"
            parts.append(f"{label} {_current_text(change)}→{_proposed_text(change)}")
        return ", ".join(parts)
    for operation in operations:
        if isinstance(operation, AddHorizontalPodAutoscaler):
            target = f" @{operation.target_cpu_utilization}% cpu" if operation.target_cpu_utilization else ""
            return f"{operation.min_replicas}-{operation.max_replicas} replicas{target}"
        if isinstance(operation, DeleteResource):
            return context.plan.action.target.kind
    return ""


def _title_savings(context: PullRequestContext) -> list[str]:
    totals = savings_totals(context.recommendations)
    parts: list[str] = []
    if totals.has_cost and totals.cost_annual > 0:
        parts.append(f"−{fmt.money_compact(totals.cost_annual, totals.currency)}/yr")
    if totals.has_co2e and totals.co2e_annual_g > 0:
        parts.append(f"−{fmt.co2e_compact(totals.co2e_annual_g)}/yr")
    if raises_requests(context) and totals.has_cost and totals.cost_annual < 0:
        # A reliability increase (H1) costs money: its cost is shown as such, never as a saving.
        parts.append(f"+{fmt.money_compact(-totals.cost_annual, totals.currency)}/yr")
    return parts


def _current_text(change: ResourceChange) -> str:
    if change.current is None:
        return "?"
    return fmt.cpu(change.current) if change.is_cpu else fmt.memory(change.current)


def _proposed_text(change: ResourceChange) -> str:
    if change.proposed is None:
        return change.proposed_text
    return fmt.cpu(change.proposed) if change.is_cpu else fmt.memory(change.proposed)


# --------------------------------------------------------------------------- #
# Commit message                                                               #
# --------------------------------------------------------------------------- #


def render_commit_message(
    context: PullRequestContext,
    settings: PullRequestRenderSettings | None = None,
    *,
    title: str | None = None,
) -> str:
    """Commit message of the commit GreenKube creates in the GitOps repository.

    The ``GreenKube-Plan`` and ``GreenKube-Recommendations`` trailers let
    ``git log`` and audits find the plan behind any commit of the target repo.
    """
    plan = context.plan
    subject = title or render_title(context, settings)
    digest = (plan.digest or "")[:19]
    if context.is_rollback:
        description = f"Rollback of GreenKube plan {plan.rollback_of_plan_id} ({digest})."
    else:
        description = f"Apply GreenKube plan {plan.plan_id} ({digest})."
    recommendation_ids = ", ".join(str(item) for item in plan.recommendation_ids)
    trailers = [f"GreenKube-Plan: {plan.plan_id}", f"GreenKube-Recommendations: {recommendation_ids}"]
    if context.is_rollback:
        trailers.append(f"GreenKube-Rollback-Of: {plan.rollback_of_plan_id}")
    return f"{subject}\n\n{description}\n\n" + "\n".join(trailers) + "\n"


# --------------------------------------------------------------------------- #
# Body                                                                         #
# --------------------------------------------------------------------------- #


def render_body(
    context: PullRequestContext,
    settings: PullRequestRenderSettings | None = None,
    *,
    title: str | None = None,
) -> str:
    """Render the body through the configured template, at most ``max_body_chars`` long.

    Sections 1-7 are each capped and always kept; the diffs get the remaining
    space and are truncated first. The footer is always last.
    """
    selected = settings or PullRequestRenderSettings()
    footer = render_footer(context)
    sections = {
        "summary": _summary(context, selected),
        "changes": _changes(context),
        "why": _why(context, selected),
        "risk": _risk(context),
        "assumptions": _assumptions(context),
        "rollback": _rollback(context, selected),
        "verification": _verification(context, selected),
        "provenance": _provenance(context),
    }
    scalars = _scalars(context, selected, title or render_title(context, selected))
    # The template's own text and the scalars are fixed; the sections share what is
    # left, so a long operator template cannot push sections 1-7 past the hard cut.
    literal = render_template(selected.template, {**scalars, **{name: "" for name in sections}, "diffs": ""})
    referenced = [name for name in sections if name in _referenced_sections(selected.template)]
    available = selected.max_body_chars - len(literal) - len(footer) - 2 * len(referenced) - _BODY_MARGIN
    _fit_sections(sections, referenced, available)
    values = {**scalars, **sections, "diffs": ""}
    without_diffs = render_template(selected.template, values)
    budget = selected.max_body_chars - len(without_diffs) - len(footer) - 2
    values["diffs"] = _diffs(context, budget)
    body = render_template(selected.template, values)
    limit = selected.max_body_chars - len(footer) - 1
    if len(body) > limit:
        note = "\n\n_The description was truncated to the provider limit._\n"
        body = body[: max(limit - len(note), 0)].rstrip() + note
    return f"{body}\n{footer}"


def render_footer(context: PullRequestContext) -> str:
    """``<!-- greenkube:v1 {...} -->`` with the plan identity, safe inside an HTML comment."""
    plan = context.plan
    payload = {
        "plan_id": plan.plan_id,
        "recommendation_ids": list(plan.recommendation_ids),
        "revision": context.base_revision or None,
        "change_set_id": plan.change_set_id,
        "digest": plan.digest,
    }
    if plan.rollback_of_plan_id is not None:
        payload["rollback_of_plan_id"] = plan.rollback_of_plan_id
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    # ``--`` cannot appear inside an HTML comment; JSON accepts the escaped form.
    encoded = encoded.replace("--", "-\\u002d").replace(">", "\\u003e")
    return f"{FOOTER_PREFIX}{encoded} -->"


def parse_footer(body: str) -> dict[str, Any] | None:
    """Read the ``greenkube:v1`` footer back from a PR body (``None`` when absent or invalid).

    Only the footer on the body's last line counts; a ``greenkube:v1`` comment
    anywhere else (a diff, a recommendation reason) is ignored.
    """
    match = _FOOTER.search(body or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _scalars(context: PullRequestContext, settings: PullRequestRenderSettings, title: str) -> dict[str, str]:
    plan = context.plan
    return {
        "title": title,
        "plan_id": plan.plan_id,
        "digest": plan.digest or "",
        "workload": context.workload,
        "namespace": plan.action.target.namespace,
        "repository": context.repository,
        "branch": context.branch,
        "recommendation_ids": ", ".join(f"#{item}" for item in plan.recommendation_ids),
        "risk_level": highest_risk(context.recommendations) or "unknown",
        "plan_url": settings.link(f"changes/{plan.plan_id}") or "",
    }


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    note = "\n\n_… section truncated to fit the provider limit._"
    return text[: max(limit - len(note), 0)].rstrip() + note


def _referenced_sections(template: str) -> set[str]:
    return {match.group(1) for match in PLACEHOLDER.finditer(template)}


def _fit_sections(sections: dict[str, str], names: Sequence[str], budget: int) -> None:
    """Cap ``names`` so together they fit ``budget``: short sections keep their text, long ones share the rest.

    No section exceeds :data:`MAX_SECTION_CHARS`, and none is cut below
    :data:`_MIN_SECTION_CHARS` (its heading and first lines always remain).
    """
    remaining = sorted(names, key=lambda name: len(sections[name]))
    left = max(budget, 0)
    for index, name in enumerate(remaining):
        share = left // (len(remaining) - index)
        sections[name] = _cap(sections[name], max(min(MAX_SECTION_CHARS, share), _MIN_SECTION_CHARS))
        left -= len(sections[name])


def _summary(context: PullRequestContext, settings: PullRequestRenderSettings) -> str:
    plan = context.plan
    target = plan.action.target
    ids = ", ".join(_recommendation_ref(item, settings) for item in plan.recommendation_ids)
    if context.is_rollback:
        sentence = (
            f"GreenKube proposes to **revert plan `{plan.rollback_of_plan_id}`** on "
            f"`{target.namespace}/{target.kind}/{target.name}` after its recommendations entered rollback review "
            f"({ids})."
        )
    else:
        details = _title_details(context)
        sentence = (
            f"GreenKube proposes to **{plan_verb(context)}** `{target.namespace}/{target.kind}/{target.name}`"
            + (f" ({details})" if details else "")
            + f", from recommendation{'s' if len(plan.recommendation_ids) > 1 else ''} {ids}."
        )
    lines = ["## Summary", "", sentence]
    if raises_requests(context):
        lines.extend(("", *_increase_totals_table(context), "", *_reliability_lines(context)))
    elif not context.is_rollback:
        lines.extend(("", *_totals_table(context)))
    if not context.is_rollback:
        capacity = _capacity_lines(context)
        if capacity:
            lines.extend(("", *capacity))
    lines.extend(_oom_history_unknown_lines(context, settings))
    if context.missing_recommendation_ids:
        missing = ", ".join(f"#{item}" for item in context.missing_recommendation_ids)
        lines.extend(
            ("", f"> Recommendation evidence is unavailable for {missing}; review those records before merging.")
        )
    return "\n".join(lines)


def _oom_history_unknown_lines(context: PullRequestContext, settings: PullRequestRenderSettings) -> list[str]:
    """A warning per recommendation whose OOM history is unknown (opened by a person, never automatically)."""
    from greenkube.core.optimization.risks import oom_history_unknown_warning

    lines: list[str] = []
    for record in context.recommendations:
        texts = oom_history_unknown_warning(record.risk_factors)
        if texts is None:
            continue
        message, check = texts
        ref = _recommendation_ref(record.id, settings) if record.id is not None else "this recommendation"
        lines.extend(
            (
                "",
                f"> [!WARNING]\n> **OOM history unknown ({ref}).** {fmt.inline(message, max_chars=600)}\n"
                f"> {fmt.inline(check, max_chars=600)}",
            )
        )
    return lines


def _recommendation_ref(recommendation_id: int, settings: PullRequestRenderSettings) -> str:
    link = settings.link(f"recommendations/{recommendation_id}")
    return f"[#{recommendation_id}]({link})" if link else f"#{recommendation_id}"


def _totals_table(context: PullRequestContext) -> list[str]:
    totals = savings_totals(context.recommendations)
    if not totals.has_cost and not totals.has_co2e:
        return ["_No projected savings are recorded for these recommendations._"]
    factors = totals.realization_factors
    if len(factors) == 1:
        realization = f" (realization factor {factors[0]:.2f})"
    elif factors:
        realization = f" (realization factor {min(factors):.2f}–{max(factors):.2f})"
    else:
        realization = ""
    lines = [
        f"| Projected saving{realization} | per month | per year |",
        "| --- | ---: | ---: |",
    ]
    if totals.has_cost:
        lines.append(
            f"| Cost ({totals.currency}) | {fmt.money(totals.cost_annual / 12, totals.currency)} "
            f"| {fmt.money(totals.cost_annual, totals.currency)} |"
        )
    allocated = totals.allocated_cost_annual
    fixed_nodes = bool(factors) and all(factor == 0 for factor in factors)
    if allocated is not None and allocated > 0 and abs(allocated - totals.cost_annual) > 0.005:
        # IMP-03: the allocated saving (chargeback/showback) is always shown, never hidden behind a 0.
        lines.append(
            f"| Allocated cost (chargeback, not billed) ({totals.currency}) "
            f"| {fmt.money(allocated / 12, totals.currency)} | {fmt.money(allocated, totals.currency)} |"
        )
    if totals.has_co2e:
        lines.append(f"| CO₂e | {fmt.co2e(totals.co2e_annual_g / 12)} | {fmt.co2e(totals.co2e_annual_g)} |")
    projector = (
        "the Impact Model"
        if any(isinstance(record.impact, dict) for record in context.recommendations)
        else "the recommendation sources"
    )
    notes = [f"Projected by {projector}; measured savings are reported on this pull request after the rollout."]
    if fixed_nodes and allocated is not None and allocated > 0:
        notes.append("Fixed nodes: frees capacity, bill unchanged until nodes are removed.")
    if len(totals.currencies) > 1:
        notes.append(
            f"Only {totals.currency} amounts are totalled; other currencies: {', '.join(totals.currencies[1:])}."
        )
    if totals.mixed_currency_records:
        count = totals.mixed_currency_records
        notes.append(
            f"The cost of {count} recommendation{'s' if count > 1 else ''} priced in several currencies is not "
            "totalled; see the change page for its breakdown."
        )
    return [*lines, "", *(f"_{note}_" for note in notes)]


def _increase_totals_table(context: PullRequestContext) -> list[str]:
    """Projected cost of a reliability increase (H1): positive amounts, labelled as cost, never as a saving."""
    totals = savings_totals(context.recommendations)
    if not totals.has_cost and not totals.has_co2e:
        return ["_No projected cost is recorded for these recommendations._"]
    lines = ["| Projected cost (reliability fix) | per month | per year |", "| --- | ---: | ---: |"]
    if totals.has_cost:
        cost = -totals.cost_annual
        lines.append(
            f"| Cost ({totals.currency}) | +{fmt.money(cost / 12, totals.currency)} "
            f"| +{fmt.money(cost, totals.currency)} |"
        )
    if totals.has_co2e:
        co2e = -totals.co2e_annual_g
        lines.append(f"| CO₂e | {_signed_co2e(co2e / 12)} | {_signed_co2e(co2e)} |")
    return [
        *lines,
        "",
        "_Projected by the Impact Model; a request increase reserves more capacity, so it is reported as a cost, "
        "never as a saving._",
    ]


def _signed(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value >= 0 else f"−{-value:.{digits}f}"


def _capacity_lines(context: PullRequestContext) -> list[str]:
    """Projected released capacity (WP-08.1): its own table, in its own units, never added to cost savings."""
    totals = capacity_totals(context.recommendations)
    if totals.empty:
        return []
    lines = ["| Released capacity (projected) | now | per year |", "| --- | ---: | ---: |"]
    if totals.cpu_cores is not None or totals.cpu_core_hours is not None:
        now = f"{_signed(totals.cpu_cores)} cores" if totals.cpu_cores is not None else "–"
        year = f"{_signed(totals.cpu_core_hours, 0)} core-hours" if totals.cpu_core_hours is not None else "–"
        lines.append(f"| CPU requests | {now} | {year} |")
    if totals.mem_gib is not None or totals.mem_gib_hours is not None:
        now = f"{_signed(totals.mem_gib)} GiB" if totals.mem_gib is not None else "–"
        year = f"{_signed(totals.mem_gib_hours, 0)} GiB-hours" if totals.mem_gib_hours is not None else "–"
        lines.append(f"| Memory requests | {now} | {year} |")
    if totals.storage_gib is not None:
        lines.append(f"| Storage | {_signed(totals.storage_gib, 1)} GiB | – |")
    if totals.load_balancers is not None:
        lines.append(f"| LoadBalancers | {_signed(totals.load_balancers, 0)} | – |")
    return [
        *lines,
        "",
        '_Reserved capacity, not money: never added to the cost saving. "now" is (current − proposed request) × '
        'mean replicas; "per year" is annualized over the observed coverage. Negative values are increases._',
    ]


def _signed_co2e(grams: float) -> str:
    return f"+{fmt.co2e(grams)}" if grams >= 0 else f"−{fmt.co2e(-grams)}"


def _reliability_lines(context: PullRequestContext) -> list[str]:
    """What the increase protects against (risk reduced), from each recommendation's evidence."""
    lines = [
        "**Reliability fix.** This pull request raises requests to reduce the risk of OOM kills, CPU contention and "
        "evictions; it is never approved automatically unless `automation.autoApprove.increases` allows it.",
    ]
    for record in context.recommendations:
        evidence = record.evidence
        if evidence is None:
            continue
        container = f"`{fmt.inline(record.container_name or 'workload', max_chars=80)}`"
        facts: list[str] = []
        if evidence.oom_events:
            facts.append(f"{evidence.oom_events} OOM kill{'s' if evidence.oom_events > 1 else ''}")
        memory = evidence.memory_usage
        if record.recommended_memory_request_bytes is not None and memory is not None:
            current = evidence.current.memory_request_bytes
            facts.append(
                f"memory p99 {fmt.memory(memory.p99)} · max {fmt.memory(memory.max)}"
                + (f" for a {fmt.memory(current)} request" if current else "")
            )
        cpu_usage = evidence.cpu_usage
        if record.recommended_cpu_request_millicores is not None and cpu_usage is not None:
            current = evidence.current.cpu_request_millicores
            facts.append(
                f"CPU p99 {fmt.cpu(cpu_usage.p99)}" + (f" for a {fmt.cpu(current)} request" if current else "")
            )
        if evidence.cpu_throttling_ratio:
            facts.append(f"CPU throttled in {fmt.percent(evidence.cpu_throttling_ratio)} of periods")
        if facts:
            lines.append(f"- {container}: {', '.join(facts)} in the observation window.")
    return lines


def _changes(context: PullRequestContext) -> str:
    plan = context.plan
    target = plan.action.target
    lines = ["## Changes", ""]
    if context.is_rollback:
        lines.append(
            f"Restores the resources recorded on `{target.namespace}/{target.kind}/{target.name}` before plan "
            f"`{plan.rollback_of_plan_id}` was applied:"
        )
        for operation in plan.action.operations:
            spec = operation_kind_of(operation)
            if spec is None:
                continue
            section = spec.pr_section([operation])
            for subject in sorted(spec.subjects([operation])):
                applied = ", ".join(
                    f"{change.field} {change.proposed}" for change in section.changes if change.subject == subject
                )
                lines.append(f"- {spec.pr_subject} `{subject}`: reverts {applied or 'the planned values'}")
        return "\n".join(lines)
    changes = resource_changes(context)
    if changes:
        lines.extend(("| Container | Resource | Current | Proposed | Change |", "| --- | --- | ---: | ---: | ---: |"))
        for change in changes:
            lines.append(
                f"| `{fmt.table_cell(change.container)}` | {change.field} | {_current_text(change)} "
                f"| {_proposed_text(change)} | {_delta(change)} |"
            )
    for operation in plan.action.operations:
        if isinstance(operation, DeleteResource):
            scoped = f"{target.namespace}/{target.name}" if target.namespace else target.name
            lines.append(f"- Delete `{target.kind}` `{scoped}` from the repository.")
        elif isinstance(operation, AddHorizontalPodAutoscaler):
            utilization = (
                f", target CPU utilization {operation.target_cpu_utilization}%"
                if operation.target_cpu_utilization
                else ""
            )
            lines.append(
                f"- Add a HorizontalPodAutoscaler for `{target.kind}/{target.name}`: "
                f"{operation.min_replicas}–{operation.max_replicas} replicas{utilization}."
            )
    if len(lines) == 2:
        lines.append("_No resource values change; see the diffs._")
    return "\n".join(lines)


def _delta(change: ResourceChange) -> str:
    if change.current is None or change.proposed is None:
        return "n/a"
    difference = change.proposed - change.current
    amount = fmt.cpu(abs(difference)) if change.is_cpu else fmt.memory(abs(difference))
    sign = "+" if difference > 0 else "−"
    ratio = f" ({difference / change.current:+.0%})" if change.current else ""
    return f"{sign}{amount}{ratio}"


def _why(context: PullRequestContext, settings: PullRequestRenderSettings) -> str:
    lines = ["## Why", ""]
    if not context.recommendations:
        lines.append("_The recommendation records behind this plan could not be loaded._")
        return "\n".join(lines)
    for record in context.recommendations:
        lines.extend(_why_record(record, settings))
        lines.append("")
    return "\n".join(lines).rstrip()


def _why_record(record: RecommendationRecord, settings: PullRequestRenderSettings) -> list[str]:
    capability = getattr(record.capability, "value", record.capability) or record.type.value
    target = (
        f"`{fmt.inline(record.namespace or 'cluster', max_chars=80)}/"
        f"{fmt.inline(record.owner_kind or 'resource', max_chars=60)}/"
        f"{fmt.inline(record.owner_name or record.pod_name or 'unknown', max_chars=120)}`"
    ) + (f" · container `{fmt.inline(record.container_name, max_chars=80)}`" if record.container_name else "")
    reference = _recommendation_ref(record.id, settings) if record.id is not None else "recommendation"
    lines = [f"**{reference} · {capability}** — {target}"]
    finding = fmt.inline(record.reason or record.description)
    if finding:
        lines.append(f"- Finding: {finding}")
    sources = [source for source in record.sources if source] or [getattr(record.source, "value", str(record.source))]
    consensus = f"- Consensus: {len(sources)} source{'s' if len(sources) > 1 else ''} ({', '.join(sources)})"
    if record.superseded_by:
        consensus += f"; `{fmt.inline(record.superseded_by, max_chars=80)}` won arbitration"
    proposal = _proposal_text(record)
    if proposal:
        consensus += f"; proposed {proposal}"
    lines.append(consensus)
    evidence = record.evidence
    if evidence is None:
        lines.append("- Evidence: not stored with this recommendation.")
    else:
        window = ""
        if evidence.observation_window_start and evidence.observation_window_end:
            hours = (evidence.observation_window_end - evidence.observation_window_start).total_seconds() / 3600
            window = (
                f"{fmt.duration_hours(round(hours))} ({fmt.timestamp(evidence.observation_window_start)} → "
                f"{fmt.timestamp(evidence.observation_window_end)}), "
            )
        lines.append(
            f"- Evidence: {window}{evidence.sample_count} samples, {fmt.percent(evidence.coverage_ratio)} coverage"
        )
        if evidence.cpu_usage is not None:
            usage = evidence.cpu_usage
            lines.append(f"- CPU usage: p95 {fmt.cpu(usage.p95)} · p99 {fmt.cpu(usage.p99)} · max {fmt.cpu(usage.max)}")
        if evidence.memory_usage is not None:
            usage = evidence.memory_usage
            lines.append(
                f"- Memory usage: p95 {fmt.memory(usage.p95)} · p99 {fmt.memory(usage.p99)} · "
                f"max {fmt.memory(usage.max)}"
            )
        stability = []
        if evidence.restart_count is not None:
            stability.append(f"{evidence.restart_count} restarts")
        if evidence.oom_events is not None:
            stability.append(f"{evidence.oom_events} OOM events")
        if stability:
            lines.append(f"- Stability: {', '.join(stability)} in the window")
    if record.confidence is not None:
        lines.append(f"- Confidence: {fmt.percent(record.confidence)}")
    link = settings.link(f"recommendations/{record.id}") if record.id is not None else None
    if link:
        lines.append(f"- Chart and full evidence: [GreenKube]({link})")
    return lines


def _proposal_text(record: RecommendationRecord) -> str:
    evidence = record.evidence
    parts = []
    cpu_value = record.recommended_cpu_request_millicores
    memory_value = record.recommended_memory_request_bytes
    if evidence is not None:
        cpu_value = cpu_value if cpu_value is not None else evidence.proposed.cpu_request_millicores
        memory_value = memory_value if memory_value is not None else evidence.proposed.memory_request_bytes
    if cpu_value is not None:
        parts.append(f"cpu {fmt.cpu(cpu_value)}")
    if memory_value is not None:
        parts.append(f"memory {fmt.memory(memory_value)}")
    return ", ".join(parts)


def _risk(context: PullRequestContext) -> str:
    risk = highest_risk(context.recommendations) or "unknown"
    lines = ["## Risk & what to watch", "", f"Risk: **{risk}**."]
    factors: list[str] = []
    for record in context.recommendations:
        for factor in record.risk_factors:
            text = fmt.inline(factor, max_chars=300)
            if text and text not in factors:
                factors.append(text)
    if factors:
        lines.extend(("", "Risk factors:", *(f"- {factor}" for factor in factors[:20])))
    headroom = _headroom_lines(context)
    if headroom:
        lines.extend(("", "Headroom over observed usage:", *headroom))
    interactions = _interaction_notes(context)
    if interactions:
        lines.extend(
            ("", "Autoscalers and disruption budgets (HPA, VPA, PDB):", *(f"- {note}" for note in interactions))
        )
    watch: list[str] = []
    for record in context.recommendations:
        if record.evidence is None:
            continue
        for condition in record.evidence.rollback_conditions:
            text = condition.description or f"{condition.metric} {condition.comparator} {condition.threshold:g}"
            text = fmt.inline(text, max_chars=300)
            if text not in watch:
                watch.append(text)
    watch.extend(item for item in _DEFAULT_WATCH if item not in watch)
    lines.extend(("", "Watch these for 24h after the rollout:", *(f"- {item}" for item in watch[:12])))
    return "\n".join(lines)


def _interaction_notes(context: PullRequestContext) -> list[str]:
    """HPA, VPA and PDB notes from the plan's policy checks and warnings, the risk factors and the action."""
    notes: list[str] = []

    def add(note: str) -> None:
        text = fmt.inline(note, max_chars=400)
        if text and text not in notes:
            notes.append(text)

    plan = context.plan
    for operation in plan.action.operations:
        if isinstance(operation, AddHorizontalPodAutoscaler):
            add(
                f"This plan adds a HorizontalPodAutoscaler ({operation.min_replicas}–{operation.max_replicas} "
                "replicas). Its CPU target is a share of the container requests, so later request changes move the "
                "scaling point; no other autoscaler (HPA, KEDA, a VPA in an active mode) should control the "
                "replicas, and a PodDisruptionBudget must still allow evictions at the minimum replica count."
            )
    policy = plan.policy_result if isinstance(plan.policy_result, dict) else {}
    checks = policy.get("checks") if isinstance(policy.get("checks"), list) else []
    checked: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("gate") not in _INTERACTION_GATES:
            continue
        gate = str(check["gate"])
        checked.update(_INTERACTION_GATES[gate])
        state = str(check.get("state") or "")
        message = str(check.get("message") or "").strip()
        names = _interaction_names(check.get("observed"))
        suffix = f" ({', '.join(names)})" if names else ""
        prefix = _INTERACTION_LABELS[gate]
        if state == "not_available":
            add(f"{prefix}: not checked. {message}")
        elif message:
            add(f"{prefix} ({state or 'unknown'}): {message.rstrip('.')}{suffix}.")
    warnings = policy.get("warnings") if isinstance(policy.get("warnings"), list) else []
    for warning in warnings:
        detail = warning.get("detail") if isinstance(warning, dict) else None
        if isinstance(detail, str) and _INTERACTION_WORDS.search(detail):
            add(f"Policy warning: {detail}")
    for record in context.recommendations:
        for factor in record.risk_factors:
            explanation = _INTERACTION_FACTORS.get(str(factor))
            if explanation is not None:
                add(explanation)
    if not context.is_rollback and plan.action.operations and not checked:
        add(
            "The policy result carries no HPA, VPA or PDB evidence for this workload; check the autoscalers and "
            "disruption budgets that target it before merging."
        )
    return notes[:10]


def _interaction_names(observed: object) -> list[str]:
    names: list[str] = []
    candidates: list[object] = []
    if isinstance(observed, dict):
        candidates.extend(observed.get("hpas") or ())
    elif isinstance(observed, list):
        candidates.extend(item.get("hpa") or item.get("vpa") for item in observed if isinstance(item, dict))
    for item in candidates:
        if isinstance(item, str) and item and item not in names:
            names.append(fmt.inline(item, max_chars=80))
    return names[:5]


_INTERACTION_GATES = {
    "hpa_interaction": ("hpa",),
    "vpa_interaction": ("vpa",),
    "pdb_interaction": ("pdb",),
    "hpa_vpa_pdb_interactions": ("hpa", "vpa", "pdb"),
}
_INTERACTION_LABELS = {
    "hpa_interaction": "HPA",
    "vpa_interaction": "VPA",
    "pdb_interaction": "PDB",
    "hpa_vpa_pdb_interactions": "HPA/VPA/PDB",
}
_INTERACTION_WORDS = re.compile(r"\b(?:HPA|VPA|PDB|autoscaler|PodDisruptionBudget)", re.IGNORECASE)
#: Risk-factor codes of the risk assessor (core.optimization.risks) that concern autoscalers or PDBs.
_INTERACTION_FACTORS = {
    "hpa_scales_on_resource": (
        "An HPA scales this workload on a resource this change modifies: its utilization target is relative to the "
        "request, so expect the replica count to move after the rollout."
    ),
    "hpa_present": "An HPA manages this workload; review its replica range after the change.",
    "vpa_conflicting_writer": (
        "A VPA in an active update mode writes this workload's requests and can override this change."
    ),
    "pdb_blocks_disruption": "A PodDisruptionBudget may block the rolling restart this change requires.",
    "additive_autoscaling": "The autoscaler is additive: it changes replica counts, not container requests.",
}


def _headroom_lines(context: PullRequestContext) -> list[str]:
    lines: list[str] = []
    if context.is_rollback:
        return lines
    for change in resource_changes(context):
        if not change.field.startswith("requests.") or change.proposed is None:
            continue
        for record in context.recommendations:
            if record.evidence is None or record.container_name not in (None, change.container):
                continue
            usage = record.evidence.cpu_usage if change.is_cpu else record.evidence.memory_usage
            if usage is None or usage.p95 <= 0:
                continue
            observed = fmt.cpu(usage.p95) if change.is_cpu else fmt.memory(usage.p95)
            line = (
                f"- `{change.container}` {change.resource}: proposed {_proposed_text(change)} is "
                f"{change.proposed / usage.p95:.2f}× the p95 ({observed})"
            )
            if usage.max > 0:
                line += f" and {change.proposed / usage.max:.2f}× the max"
            if line not in lines:
                lines.append(line)
            break
    return lines


def _assumptions(context: PullRequestContext) -> str:
    lines = ["## Assumptions", ""]
    impacts = [record.impact for record in context.recommendations if isinstance(record.impact, dict)]
    if not impacts:
        methods = sorted(
            {record.evidence.savings_method for record in context.recommendations if record.evidence is not None}
        )
        lines.append(
            "No Impact Model projection is stored with these recommendations; the savings are the sources' own "
            f"estimates (method: {', '.join(methods) or 'unknown'})."
        )
        return "\n".join(lines)
    impact = impacts[0]
    assumptions = impact.get("assumptions") if isinstance(impact.get("assumptions"), dict) else {}
    prices = assumptions.get("unit_prices") if isinstance(assumptions.get("unit_prices"), list) else []
    if prices:
        lines.append("- Unit prices:")
        for price in prices[:3]:
            if not isinstance(price, dict):
                continue
            currency = price.get("currency") or impact.get("currency") or "USD"
            detail = f" ({fmt.inline(price.get('detail'), max_chars=80)})" if price.get("detail") else ""
            lines.append(
                f"  - {fmt.unit_price(_number(price.get('cpu_core_hour')), currency)}/core-hour, "
                f"{fmt.unit_price(_number(price.get('ram_gib_hour')), currency)}/GiB-hour, "
                f"source `{fmt.inline(price.get('source') or 'unknown', max_chars=40)}`{detail}"
            )
    cost_sources = assumptions.get("observed_cost_sources")
    if isinstance(cost_sources, dict) and cost_sources:
        lines.append(f"- Observed cost sources: {', '.join(f'`{key}`' for key in sorted(cost_sources))}")
    elif assumptions.get("cost_source"):
        lines.append(f"- Cost source: `{fmt.inline(assumptions['cost_source'], max_chars=40)}`")
    intensity = _number(assumptions.get("grid_intensity_g_per_kwh"))
    if intensity is not None:
        power = assumptions.get("power_sources")
        power_text = f"; power model {', '.join(str(item) for item in power)}" if isinstance(power, list) else ""
        lines.append(f"- Carbon intensity: {intensity:,.0f} gCO₂e/kWh ({_intensity_source(assumptions)}){power_text}")
    basis = assumptions.get("co2e_basis")
    if isinstance(basis, str) and basis:
        lines.append(f"- CO₂e basis: `{fmt.inline(basis, max_chars=60)}`")
    pue = _number(assumptions.get("pue"))
    if pue is not None:
        lines.append(f"- PUE: {pue:.2f}")
    factor = _number(impact.get("realization_factor"))
    if factor is not None:
        source = assumptions.get("realization_factor_source") or "config"
        lines.append(f"- Realization factor: {factor:.2f} (`{fmt.inline(source, max_chars=60)}`)")
        reasons = assumptions.get("realization_reasons")
        if isinstance(reasons, list):
            lines.extend(f"  - {fmt.inline(reason, max_chars=200)}" for reason in reasons[:5])
    carbon_price = _number(impact.get("carbon_price_per_tonne"))
    if carbon_price is not None:
        lines.append(f"- Carbon price: {fmt.money(carbon_price, impact.get('currency'))}/tCO₂e (ranking only)")
    if assumptions.get("unknown_node_profile"):
        lines.append("- At least one node has no power profile; a default profile was used.")
    versions = sorted({str(item.get("model_version")) for item in impacts if item.get("model_version")})
    lines.append(f"- Impact Model version: {', '.join(versions) or 'unknown'}")
    return "\n".join(lines)


def _intensity_source(assumptions: dict[str, Any]) -> str:
    """Where the intensity came from: the projection's recorded sources, else how the model derives it."""
    sources = assumptions.get("intensity_sources")
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list):
        single = assumptions.get("intensity_source")
        sources = [single] if isinstance(single, str) else []
    names = [fmt.inline(item, max_chars=40) for item in sources if isinstance(item, str) and item.strip()]
    if names:
        return f"source {', '.join(f'`{name}`' for name in names[:5])}"
    return "source: stored grid intensity of the observed samples, node-profile intensity where a sample has none"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rollback(context: PullRequestContext, settings: PullRequestRenderSettings) -> str:
    plan = context.plan
    lines = ["## Rollback", ""]
    if context.is_rollback:
        lines.append(f"This pull request is itself the rollback of plan `{plan.rollback_of_plan_id}`.")
        lines.append("To undo it, revert this pull request.")
        return "\n".join(lines)
    lines.append("- Revert this pull request; the GitOps controller syncs the previous values.")
    automated = plan.source.renderer in PLANNABLE_RENDERERS and all(
        (spec := operation_kind_of(operation)) is not None and spec.supports_rollback([operation])
        for operation in plan.action.operations
    )
    if automated:
        link = settings.link(f"changes/{plan.plan_id}")
        where = f" from [the change page]({link})" if link else ""
        lines.append(
            "- Or let GreenKube open the rollback pull request"
            f"{where}: when a health gate fails after the rollout, the recommendation enters rollback review and "
            "the inverse plan restores the recorded pre-apply values."
        )
    elif edits_limits(context):
        lines.append(
            "- This pull request changes container limits, which the recorded pre-apply baseline does not hold: "
            "GreenKube cannot build an automated rollback; revert this pull request manually."
        )
    else:
        lines.append(
            "- GreenKube cannot build an automated rollback for this layout or action kind; revert this pull "
            "request manually."
        )
    return "\n".join(lines)


def _verification(context: PullRequestContext, settings: PullRequestRenderSettings) -> str:
    plan = context.plan
    source = plan.source
    controller = _CONTROLLER_NAMES.get(source.controller_kind, source.controller_kind.value)
    lines = [
        "## Verification plan",
        "",
        f"1. After the merge, GreenKube waits for the {controller} `{source.controller_namespace}/"
        f"{source.controller_name}` to sync the merge commit, then for the rollout to complete.",
    ]
    if context.is_rollback:
        lines.append("2. The rollback is confirmed when the workload runs the restored values again.")
    elif settings.change_measurement_enabled:
        lines.append(
            f"2. Measurement starts after a {fmt.duration_hours(settings.measurement_warmup_hours)} warm-up and "
            f"lasts {fmt.duration_hours(settings.measurement_post_window_days * 24)}; it is compared with a "
            f"baseline of {fmt.duration_hours(settings.measurement_pre_window_days * 24)} that ends at the merge, "
            "matched hour of the week and normalized for load, with a 90% confidence interval."
        )
        if raises_requests(context):
            lines.append(
                "3. A reliability increase is judged on reliability: GreenKube compares the OOM-kill rate after the "
                "rollout with the rate observed before it, and reports the measured cost as a cost. Health gates "
                "(restarts, throttling, readiness, and OOM kills that do not drop) can send the change to rollback "
                "review."
            )
        else:
            lines.append(
                "3. The outcome is `realized`, `not_realized`, `inconclusive` or `contaminated`; health gates "
                "(restarts, OOM kills, throttling, readiness) can send the change to rollback review."
            )
    else:
        lines.append(
            f"2. The verifier compares {fmt.duration_hours(settings.verification_window_hours)} after the rollout "
            "with the same length before the merge, and checks restarts, readiness and throttling."
        )
    where = []
    if settings.lifecycle_comments:
        where.append("a GreenKube status comment on this pull request (updated at each step)")
    ledger = settings.link(f"ledger?change={plan.plan_id}")
    if ledger:
        where.append(f"[the savings ledger]({ledger})")
    lines.append(f"\nResults are posted to {' and '.join(where) if where else 'the GreenKube savings ledger'}.")
    return "\n".join(lines)


def _provenance(context: PullRequestContext) -> str:
    plan = context.plan
    source = plan.source
    file_plan = plan.file_plan
    base_revision = context.base_revision
    lines = [
        "<details><summary>Plan provenance</summary>",
        "",
        f"- Plan: `{plan.plan_id}`",
        f"- Digest: `{plan.digest}`",
        f"- Provider: `{source.provider_id}`",
        f"- Repository: `{context.repository}`",
        f"- Controller: `{source.controller_kind.value}/{source.controller_namespace}/{source.controller_name}`",
        f"- Renderer: `{source.renderer.value}`",
        f"- Source path: `{source.path}`",
        f"- Base branch/revision: `{source.ref}` / `{base_revision}`",
    ]
    committed = plan.committed_on_revision
    if committed is not None and base_revision and committed.lower() != base_revision.lower():
        lines.append(
            f"- Committed on moved head: `{committed}` (every file this plan touches is unchanged since the "
            "planned base revision)"
        )
    lines.append(f"- Plan branch: `{context.branch}`")
    if file_plan is not None:
        lines.append(f"- Render validation: `{'passed' if file_plan.validation.passed else 'failed'}`")
        for warning in file_plan.validation.warnings[:5]:
            lines.append(f"  - warning: {fmt.inline(warning, max_chars=200)}")
        files = ", ".join(f"`{edit.path}` ({edit.operation})" for edit in file_plan.files[:20])
        lines.append(f"- Files: {files}")
    if source.source_revision:
        lines.append(f"- Observed controller revision: `{source.source_revision}`")
        if base_revision and _revision_identity(source.source_revision) != _revision_identity(base_revision):
            lines.append(
                f"- Revision drift: this plan targets branch head `{base_revision}`, while the controller reports "
                f"`{source.source_revision}`; refresh discovery or sync before approval if the live revision is "
                "required."
            )
    policy = plan.policy_result if isinstance(plan.policy_result, dict) else {}
    if "allowed" in policy:
        warnings = policy.get("warnings") if isinstance(policy.get("warnings"), list) else []
        lines.append(f"- Policy: `{'allowed' if policy.get('allowed') else 'rejected'}`, {len(warnings)} warning(s)")
    lines.extend(("", "</details>"))
    return "\n".join(lines)


def _revision_identity(value: str) -> str:
    normalized = value.strip().casefold()
    match = re.search(r"(?:^|@sha(?:1|256):)([0-9a-f]{40}|[0-9a-f]{64})$", normalized)
    return match.group(1) if match is not None else normalized


def _diffs(context: PullRequestContext, budget: int) -> str:
    file_plan = context.plan.file_plan
    if file_plan is None:
        return ""
    if budget < _MIN_DIFF_CHARS:
        return "_The diffs are omitted to fit the provider limit; review the plan branch._"
    blocks = (("Source diff", file_plan.diff), ("Rendered diff", file_plan.rendered_diff))
    overhead = sum(len(_diff_block(name, "")) for name, _ in blocks) + 2
    per_diff = max((budget - overhead) // len(blocks), 0)
    rendered = [_diff_block(name, _truncate(diff, per_diff)) for name, diff in blocks]
    return "\n\n".join(rendered)


def _diff_block(name: str, diff: str) -> str:
    return f"<details><summary>{name}</summary>\n\n```diff\n{diff}\n```\n\n</details>"


def _truncate(value: str, limit: int) -> str:
    # Never let a diff close the fence early.
    value = value.replace("```", "`​``")
    if len(value) <= limit:
        return value
    note = f"\n... truncated after {max(limit - 80, 0)} characters; see the plan branch"
    return value[: max(limit - len(note), 0)].rstrip() + note
