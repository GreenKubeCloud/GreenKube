# src/greenkube/automation/pr_body.py
"""Renders the pull-request title and body from the recommendation evidence.

The PR is a rendered recommendation, not a separate artifact: every section is
derived from the persisted evidence block so reviewers see exactly the analysis
GreenKube used.
"""

from __future__ import annotations

from typing import Optional

from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _md(value: object) -> str:
    """Escape user-controlled text without allowing Markdown structure."""
    return (
        str(value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", " ")
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("#", "\\#")
        .replace("!", "\\!")
        .replace("|", "\\|")
        .replace("~", "\\~")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _tri_state(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _humanize_bytes(value: Optional[int]) -> str:
    if value is None:
        return "n/a"
    for suffix, factor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= factor and value % factor == 0:
            return f"{value // factor}{suffix}"
    return f"{value}B"


def recommendation_title(record: RecommendationRecord) -> str:
    """Builds the conventional PR title for a rightsizing recommendation."""
    owner = f"{record.owner_kind}/{record.owner_name}" if record.owner_kind else record.pod_name or "workload"
    if record.type == RecommendationType.RIGHTSIZING_CPU:
        current = record.current_cpu_request_millicores
        proposed = record.recommended_cpu_request_millicores
        return f"greenkube(optimization): rightsize {owner} cpu {current}m → {proposed}m"
    if record.type == RecommendationType.RIGHTSIZING_MEMORY:
        current = _humanize_bytes(record.current_memory_request_bytes)
        proposed = _humanize_bytes(record.recommended_memory_request_bytes)
        return f"greenkube(optimization): rightsize {owner} memory {current} → {proposed}"
    return f"greenkube(optimization): apply recommendation #{record.id} ({record.type.value})"


def render_pr_body(
    record: RecommendationRecord, *, diff: Optional[str] = None, source_ref: Optional[str] = None
) -> str:
    """Renders the full pull-request body from the evidence block."""
    evidence = record.evidence
    lines: list[str] = []

    lines.append("## Summary")
    lines.append("")
    lines.append(_md(record.description))
    if record.reason:
        lines.append("")
        lines.append(_md(record.reason))
    lines.append("")

    lines.append("## Impact")
    lines.append("")
    if evidence is not None and evidence.expected_savings_cost_annual is not None:
        lines.append(f"- Estimated annual cost savings: **${evidence.expected_savings_cost_annual:,.2f}**")
    if evidence is not None and evidence.expected_savings_co2e_grams_annual is not None:
        lines.append(f"- Estimated annual CO₂e savings: **{evidence.expected_savings_co2e_grams_annual:,.0f} gCO₂e**")
    method = evidence.savings_method if evidence else "unknown"
    lines.append(f"- Savings method: `{method}`")
    lines.append("")

    lines.append("## Risk & reliability")
    lines.append("")
    risk = record.risk_level.value if record.risk_level else "unknown"
    lines.append(f"- Risk level: **{risk}**")
    if record.risk_factors:
        lines.append(f"- Risk factors: {', '.join(f'`{_md(f)}`' for f in record.risk_factors)}")
    lines.append(f"- Reversible: {_tri_state(record.reversible)}")
    lines.append(f"- Requires restart: {_tri_state(record.requires_restart)}")
    lines.append("")

    if evidence is not None:
        lines.append("## Evidence")
        lines.append("")
        window_start = evidence.observation_window_start
        window_end = evidence.observation_window_end
        lines.append(f"- Observation window: {_md(window_start)} → {_md(window_end)}")
        lines.append(f"- Samples: {evidence.sample_count} (coverage {evidence.coverage_ratio:.0%})")
        lines.append("")

        if evidence.cpu_usage or evidence.memory_usage:
            lines.append("| Signal | avg | p50 | p90 | p95 | p99 | max |")
            lines.append("|---|---|---|---|---|---|---|")
            for label, stats in (("CPU (m)", evidence.cpu_usage), ("Memory (B)", evidence.memory_usage)):
                if stats is None:
                    continue
                lines.append(
                    f"| {label} | {stats.avg:.0f} | {stats.p50:.0f} | {stats.p90:.0f} | "
                    f"{stats.p95:.0f} | {stats.p99:.0f} | {stats.max:.0f} |"
                )
            lines.append("")

        lines.append("| Resource | Current request | Proposed request |")
        lines.append("|---|---|---|")
        if evidence.current.cpu_request_millicores is not None or evidence.proposed.cpu_request_millicores is not None:
            lines.append(
                f"| CPU | {evidence.current.cpu_request_millicores}m | {evidence.proposed.cpu_request_millicores}m |"
            )
        if evidence.current.memory_request_bytes is not None or evidence.proposed.memory_request_bytes is not None:
            lines.append(
                f"| Memory | {_humanize_bytes(evidence.current.memory_request_bytes)} | "
                f"{_humanize_bytes(evidence.proposed.memory_request_bytes)} |"
            )
        lines.append("")

    if diff:
        lines.append("## Proposed diff")
        lines.append("")
        lines.append("```diff")
        lines.append(diff.replace("`", "\\`"))
        lines.append("```")
        lines.append("")

    lines.append("## Verification plan")
    lines.append("")
    lines.append(
        "After merge, GreenKube observes the workload for the verification window and checks the cost, "
        "carbon and health signals against the frozen baseline."
    )
    conditions = evidence.rollback_conditions if evidence else []
    if conditions:
        lines.append("")
        for condition in conditions:
            lines.append(
                f"- `{_md(condition.metric)}` {_md(condition.comparator)} {_md(condition.threshold)}: "
                f"{_md(condition.description)}"
            )
    expires = record.expires_at.isoformat() if record.expires_at else "n/a"
    lines.append(f"- Recommendation expiry: {expires}")
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- Source: `{record.source.value if hasattr(record.source, 'value') else record.source}`")
    if record.source_ref:
        lines.append(f"- Source reference: `{_md(record.source_ref)}`")
    lines.append(f"- Recommendation ID: `{record.id}`")
    lines.append("")

    lines.append("## Reviewer checklist")
    lines.append("")
    lines.append("- [ ] Change window is acceptable for this workload")
    lines.append("- [ ] No conflicting HPA/VPA or PVC constraints")
    lines.append("- [ ] Rollback owner is identified")
    lines.append("")
    return "\n".join(lines)
