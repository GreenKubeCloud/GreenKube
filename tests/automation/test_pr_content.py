"""PR template v2: titles, bodies, footers, commit messages and templates (roadmap J1).

Rendered bodies are compared with golden files in ``snapshots/pr_bodies``;
run with ``GREENKUBE_UPDATE_SNAPSHOTS=1`` to rewrite them after a deliberate change.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from greenkube.automation.pr_content import (
    PullRequestContext,
    PullRequestRenderSettings,
    PullRequestTemplateError,
    load_template,
    parse_footer,
    render_body,
    render_commit_message,
    render_pull_request,
    render_title,
    validate_template,
)
from greenkube.automation.pr_content.formatting import co2e_compact, cpu, memory, money_compact
from greenkube.automation.pr_content.render import MAX_SECTION_CHARS
from greenkube.models.metrics import RecommendationType
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]

SNAPSHOTS = Path(__file__).parent / "snapshots" / "pr_bodies"
SETTINGS = PullRequestRenderSettings(external_url="https://greenkube.example.com")
ESSENTIAL_HEADINGS = (
    "## Summary",
    "## Changes",
    "## Why",
    "## Risk & what to watch",
    "## Assumptions",
    "## Rollback",
    "## Verification plan",
)


def _context(plan, recommendations=(), missing=()) -> PullRequestContext:
    return PullRequestContext(
        plan=plan,
        repository="acme/gitops",
        branch="greenkube/plan/0123456789abcdef0123456789abcdef",
        recommendations=tuple(recommendations),
        missing_recommendation_ids=tuple(missing),
    )


SCENARIOS = {
    "rightsize": lambda: _context(
        fx.rightsize_plan(),
        [
            fx.recommendation(41),
            fx.recommendation(42, rec_type=RecommendationType.RIGHTSIZING_MEMORY, cost=112.0, co2e_g=6_000.0),
        ],
    ),
    "delete": lambda: _context(
        fx.delete_plan(),
        [fx.recommendation(51, rec_type=RecommendationType.ZOMBIE_POD, cost=96.0, co2e_g=3_000.0, evidence=None)],
    ),
    "add_hpa": lambda: _context(
        fx.hpa_plan(),
        [fx.recommendation(61, rec_type=RecommendationType.AUTOSCALING_CANDIDATE, risk_level="medium")],
    ),
    "rollback": lambda: _context(
        fx.rollback_plan(),
        [fx.recommendation(41, status="rollback_review"), fx.recommendation(42, status="rollback_review")],
    ),
    "high_risk_missing_records": lambda: _context(
        fx.rightsize_plan(), [fx.recommendation(41, risk_level="high", impact=None)], missing=(42,)
    ),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_rendered_body_matches_snapshot_for_each_action_kind(scenario):
    content = render_pull_request(SCENARIOS[scenario](), SETTINGS)
    rendered = f"# {content.title}\n\n{content.body}"
    snapshot = SNAPSHOTS / f"{scenario}.md"
    if os.environ.get("GREENKUBE_UPDATE_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(rendered, encoding="utf-8")
    assert snapshot.exists(), f"missing snapshot {snapshot}; run with GREENKUBE_UPDATE_SNAPSHOTS=1"
    assert rendered == snapshot.read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_body_has_sections_one_to_seven_and_a_parsable_footer(scenario):
    context = SCENARIOS[scenario]()
    body = render_body(context, SETTINGS)
    positions = [body.index(heading) for heading in ESSENTIAL_HEADINGS]
    assert positions == sorted(positions)
    footer = parse_footer(body)
    assert footer is not None
    assert footer["plan_id"] == context.plan.plan_id
    assert footer["recommendation_ids"] == list(context.plan.recommendation_ids)
    assert footer["revision"] == fx.BASE_REVISION
    assert footer["digest"] == fx.PLAN_DIGEST
    assert "change_set_id" in footer
    assert body.rstrip().splitlines()[-1].startswith("<!-- greenkube:v1 {")


def test_title_reads_like_the_roadmap_example_and_respects_72_characters():
    context = SCENARIOS["rightsize"]()
    long_title = render_title(context, PullRequestRenderSettings(max_title_chars=200))
    assert long_title == "greenkube: rightsize payments/api (cpu 500m→200m, mem 1Gi→512Mi) · −$412/yr · −18 kgCO₂e/yr"

    title = render_title(context, SETTINGS)
    assert len(title) <= 72
    # Details are dropped before the savings; the full summary stays in the body.
    assert title == "greenkube: rightsize payments/api · −$412/yr · −18 kgCO₂e/yr"
    assert "cpu 500m→200m, mem 1Gi→512Mi" in render_body(context, SETTINGS)


def test_title_prefix_is_configurable_and_never_exceeds_the_limit():
    context = SCENARIOS["rightsize"]()
    assert render_title(context, PullRequestRenderSettings(title_prefix="chore(greenkube)")).startswith(
        "chore(greenkube): rightsize payments/api"
    )
    assert render_title(context, PullRequestRenderSettings(title_prefix="")).startswith("rightsize payments/api")
    long_name = fx.plan(plan_source=fx.source(workload_name="x" * 80))
    long_name = long_name.model_copy(
        update={
            "action": long_name.action.model_copy(
                update={"target": long_name.action.target.model_copy(update={"name": "x" * 80})}
            )
        }
    )
    title = render_title(_context(long_name), SETTINGS)
    assert len(title) == 72 and title.endswith("…")


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("delete", "greenkube: delete payments/api (Deployment) · −$96/yr · −3 kgCO₂e/yr"),
        ("add_hpa", "greenkube: autoscale payments/api · −$300/yr · −12 kgCO₂e/yr"),
        ("rollback", "greenkube: revert rightsize payments/api (plan pln-1)"),
    ],
)
def test_titles_per_action_kind(scenario, expected):
    assert render_title(SCENARIOS[scenario](), SETTINGS) == expected


def test_changes_list_only_the_resources_that_change():
    plan = fx.plan(
        [
            fx.SetContainerResources(
                container="api",
                resources=fx.ContainerResources(requests={"cpu": "500m", "memory": "512Mi"}),
            )
        ]
    )
    body = render_body(_context(plan, [fx.recommendation(42, rec_type=RecommendationType.RIGHTSIZING_MEMORY)]))
    changes = body.split("## Changes", 1)[1].split("## Why", 1)[0]
    assert "requests.memory | 1Gi | 512Mi | −512Mi (-50%)" in changes
    assert "requests.cpu" not in changes


def test_reliability_increase_is_not_called_a_rightsize():
    plan = fx.plan(
        [fx.SetContainerResources(container="api", resources=fx.ContainerResources(requests={"memory": "2Gi"}))]
    )
    assert render_title(_context(plan), SETTINGS).startswith("greenkube: raise resources of payments/api (mem 1Gi→2Gi)")


def test_body_never_exceeds_60k_characters_and_keeps_sections_one_to_seven():
    huge_diff = "+" + "x" * 200_000
    plan = fx.plan(diff=huge_diff, rendered_diff=huge_diff)
    records = [
        fx.recommendation(
            41,
            risk_factors=[f"factor {index} " + "y" * 250 for index in range(40)],
            reason="z" * 5_000,
        )
    ]
    body = render_body(_context(plan, records), SETTINGS)
    assert len(body) <= 60_000
    for heading in ESSENTIAL_HEADINGS:
        assert heading in body
    assert "truncated after" in body
    footer = parse_footer(body)
    assert footer is not None and footer["plan_id"] == "pln-1"
    for section in re.split(r"\n(?=## |<details>)", body):
        if section.startswith(ESSENTIAL_HEADINGS):
            assert len(section) <= MAX_SECTION_CHARS + 2


def test_diffs_are_omitted_when_the_essential_sections_fill_the_budget():
    plan = fx.plan(diff="+" + "x" * 50_000, rendered_diff="+" + "x" * 50_000)
    body = render_body(_context(plan, [fx.recommendation(41)]), PullRequestRenderSettings(max_body_chars=7_000))
    assert len(body) <= 7_000
    assert "## Summary" in body and parse_footer(body) is not None


def test_diff_content_cannot_close_its_code_fence():
    body = render_body(_context(fx.plan(diff="```\n## Injected heading")), SETTINGS)
    source_diff = body.split("<summary>Source diff</summary>", 1)[1].split("</details>", 1)[0]
    assert source_diff.count("```") == 2


def test_footer_cannot_terminate_its_html_comment():
    plan = fx.plan(plan_id="pln--evil-->")
    body = render_body(_context(plan), SETTINGS)
    footer_line = body.rstrip().splitlines()[-1]
    assert footer_line.count("-->") == 1 and footer_line.endswith(" -->")
    footer = parse_footer(body)
    assert footer is not None and footer["plan_id"] == "pln--evil-->"


def test_deep_links_use_the_external_url_and_are_absent_without_it():
    context = SCENARIOS["rightsize"]()
    linked = render_body(context, SETTINGS)
    assert "[#41](https://greenkube.example.com/recommendations/41)" in linked
    assert "https://greenkube.example.com/changes/pln-1" in linked
    assert "https://greenkube.example.com/ledger?change=pln-1" in linked
    unlinked = render_body(context, PullRequestRenderSettings())
    assert "http" not in unlinked.split("<details><summary>Plan provenance")[0]


def test_commit_message_carries_greenkube_trailers():
    message = render_commit_message(SCENARIOS["rightsize"](), SETTINGS)
    subject, _, rest = message.partition("\n\n")
    assert subject == render_title(SCENARIOS["rightsize"](), SETTINGS)
    assert rest.endswith("GreenKube-Plan: pln-1\nGreenKube-Recommendations: 41, 42\n")
    rollback = render_commit_message(SCENARIOS["rollback"](), SETTINGS)
    assert "GreenKube-Rollback-Of: pln-1" in rollback


def test_custom_template_reorders_sections_and_the_footer_is_always_appended():
    template = "### {{ title }}\n\n{{ why }}\n\nOwner: platform team\n\n{{summary}}"
    settings = PullRequestRenderSettings(template=validate_template(template))
    body = render_body(SCENARIOS["rightsize"](), settings)
    assert body.startswith("### greenkube: rightsize payments/api")
    assert body.index("## Why") < body.index("## Summary")
    assert "Owner: platform team" in body
    assert "## Assumptions" not in body
    footer = parse_footer(body)
    assert footer is not None and footer["plan_id"] == "pln-1"


def test_template_values_are_inserted_verbatim_and_never_reexpanded():
    plan = fx.plan(diff="{{ plan_id }} stays literal")
    body = render_body(_context(plan), SETTINGS)
    assert "{{ plan_id }} stays literal" in body


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{ summary }} {{ secrets }}", "unknown placeholder"),
        ("", "non-empty"),
        ("{{ summary", "unterminated"),
        ("x" * 40_000, "exceeds"),
    ],
)
def test_invalid_templates_are_rejected(template, message):
    with pytest.raises(PullRequestTemplateError, match=message):
        validate_template(template)


def test_blank_template_setting_uses_the_default_template():
    assert "{{ summary }}" in load_template("  ")


def test_config_validates_the_template_and_external_url(monkeypatch):
    from greenkube.core.config import Config

    config = Config(GITOPS_PR_TEMPLATE="{{ summary }}\n{{ diffs }}", GREENKUBE_EXTERNAL_URL="https://gk.example.com/")
    assert config.GREENKUBE_EXTERNAL_URL == "https://gk.example.com"
    settings = PullRequestRenderSettings.from_config(config)
    assert settings.template == "{{ summary }}\n{{ diffs }}"
    assert settings.link("recommendations/1") == "https://gk.example.com/recommendations/1"
    with pytest.raises(ValueError, match="unknown placeholder"):
        Config(GITOPS_PR_TEMPLATE="{{ nope }}")
    with pytest.raises(ValueError, match="GREENKUBE_EXTERNAL_URL"):
        Config(GREENKUBE_EXTERNAL_URL="greenkube.example.com")


def test_totals_never_mix_currencies():
    records = [
        fx.recommendation(41, cost=100.0),
        fx.recommendation(42, cost=80.0, impact=fx.impact(cost=80.0, co2e_g=1.0, currency="EUR")),
    ]
    body = render_body(_context(fx.rightsize_plan(), records), SETTINGS)
    assert "| Cost (EUR) | €6.67 | €80.00 |" in body
    assert "other currencies: USD" in body


def test_unit_formatting():
    assert [cpu(value) for value in (200, 1000, 1500, 12_000, 12_345)] == ["200m", "1000m", "1500m", "12", "12345m"]
    assert [memory(value) for value in (512 * fx.MIB, fx.GIB, int(1.5 * fx.GIB), 1000)] == [
        "512Mi",
        "1Gi",
        "1.5Gi",
        "1000B",
    ]
    assert [money_compact(value) for value in (4.5, 412.4, 2_300, 45_000, 1_200_000)] == [
        "$4.50",
        "$412",
        "$2.3k",
        "$45k",
        "$1.2M",
    ]
    assert [co2e_compact(value) for value in (350, 1_500, 18_000, 1_200_000)] == [
        "350 gCO₂e",
        "1.5 kgCO₂e",
        "18 kgCO₂e",
        "1.2 tCO₂e",
    ]


def test_footer_payload_is_compact_json():
    body = render_body(SCENARIOS["rightsize"](), SETTINGS)
    raw = body.rstrip().splitlines()[-1][len("<!-- greenkube:v1 ") : -len(" -->")]
    assert json.loads(raw) == parse_footer(body)
