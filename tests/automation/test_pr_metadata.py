"""Labels, draft state (J2) and CODEOWNERS routing (J3)."""

from __future__ import annotations

import pytest

from greenkube.automation.pr_content import PullRequestContext, derive_metadata, parse_codeowners
from greenkube.gitops.contracts import ContainerResources, SetContainerResources
from greenkube.models.metrics import RecommendationType
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]


def _context(plan, recommendations=()):
    return PullRequestContext(plan=plan, repository="acme/gitops", branch="b", recommendations=tuple(recommendations))


def test_default_labels_name_the_capability_and_risk():
    metadata = derive_metadata(
        _context(
            fx.rightsize_plan(),
            [fx.recommendation(41), fx.recommendation(42, rec_type=RecommendationType.RIGHTSIZING_MEMORY)],
        )
    )
    assert metadata.labels == (
        "greenkube",
        "optimization/cpu-rightsizing",
        "optimization/memory-rightsizing",
        "risk/low",
    )
    assert metadata.draft is False


def test_high_risk_plans_are_drafts_and_increases_are_labelled_reliability():
    plan = fx.plan([SetContainerResources(container="api", resources=ContainerResources(requests={"memory": "2Gi"}))])
    metadata = derive_metadata(_context(plan, [fx.recommendation(41, risk_level="high")]))
    assert metadata.draft is True
    assert metadata.risk_level == "high"
    assert metadata.labels[-2:] == ("risk/high", "reliability")
    assert (
        derive_metadata(_context(plan, [fx.recommendation(41, risk_level="high")]), draft_high_risk=False).draft
        is False
    )


def test_labels_fall_back_to_the_action_kind_and_can_be_disabled():
    assert derive_metadata(_context(fx.delete_plan())).labels == ("greenkube", "optimization/cleanup")
    assert derive_metadata(_context(fx.hpa_plan())).labels == ("greenkube", "optimization/autoscaling")
    assert derive_metadata(_context(fx.rightsize_plan(), [fx.recommendation(41)]), default_labels=False).labels == ()


def test_rollback_plans_are_never_labelled_reliability():
    metadata = derive_metadata(_context(fx.rollback_plan(), [fx.recommendation(41, value={"lane": "reliability"})]))
    assert "reliability" not in metadata.labels


GITHUB_CODEOWNERS = r"""
# Global owners
*                       @acme/platform
*.yaml                  @yaml-lover
/apps/                  @acme/apps-team
apps/payments/          @alice @acme/payments
docs/*                  docs@acme.example
/apps/payments/secret\ file.yaml
**/kustomization.yaml   @kustomize-crew
\#hash.yaml             @hash-owner
"""


@pytest.mark.parametrize(
    ("path", "owners"),
    [
        ("apps/payments/deployment.yaml", ["@alice", "@acme/payments"]),
        ("apps/orders/deployment.yaml", ["@acme/apps-team"]),
        ("README.md", ["@acme/platform"]),
        ("infra/values.yaml", ["@yaml-lover"]),
        ("docs/guide.md", ["docs@acme.example"]),
        ("docs/sub/guide.md", ["@acme/platform"]),
        ("apps/payments/secret file.yaml", []),
        ("apps/orders/overlays/prod/kustomization.yaml", ["@kustomize-crew"]),
        ("#hash.yaml", ["@hash-owner"]),
    ],
)
def test_github_codeowners_last_match_wins(path, owners):
    assert parse_codeowners(GITHUB_CODEOWNERS, "github").owners_for([path]) == owners


GITLAB_CODEOWNERS = """
* @acme/platform

[Payments] @payments-lead
apps/payments/
apps/payments/config/ @config-owner

^[Docs][2] @docs-team
*.md

[Security]
apps/**/secrets.yaml @sec
"""


def test_gitlab_sections_combine_and_apply_default_owners():
    codeowners = parse_codeowners(GITLAB_CODEOWNERS, "gitlab")
    assert codeowners.owners_for(["apps/payments/deployment.yaml"]) == ["@acme/platform", "@payments-lead"]
    assert codeowners.owners_for(["apps/payments/config/app.yaml"]) == ["@acme/platform", "@config-owner"]
    assert codeowners.owners_for(["apps/payments/README.md"]) == ["@acme/platform", "@payments-lead", "@docs-team"]
    assert codeowners.owners_for(["apps/a/b/secrets.yaml"]) == ["@acme/platform", "@sec"]
    assert codeowners.errors == ()


def test_owners_of_several_paths_are_deduplicated_in_order():
    codeowners = parse_codeowners(GITHUB_CODEOWNERS, "github")
    assert codeowners.owners_for(["apps/payments/deployment.yaml", "apps/orders/x.json", "apps/payments/y.json"]) == [
        "@alice",
        "@acme/payments",
        "@acme/apps-team",
    ]


def test_gitea_codeowners_use_anchored_regular_expressions_and_negation():
    content = """
apps/payments/.* @alice
.*\\.md$ @docs
!apps/.* @outside
"""
    codeowners = parse_codeowners(content, "gitea")
    assert codeowners.owners_for(["apps/payments/deployment.yaml"]) == ["@alice"]
    assert codeowners.owners_for(["README.md"]) == ["@docs", "@outside"]


def test_malformed_lines_are_reported_not_fatal():
    codeowners = parse_codeowners("[unterminated\n* @ok\n", "gitlab")
    assert codeowners.errors == ("line 1: invalid section header",)
    assert codeowners.owners_for(["x"]) == ["@ok"]
    invalid_regex = parse_codeowners("([ @a\n.* @b\n", "gitea")
    assert invalid_regex.owners_for(["x"]) == ["@b"]
    assert invalid_regex.errors and "invalid pattern" in invalid_regex.errors[0]


def test_role_owners_are_ignored():
    assert parse_codeowners("* @@maintainer @bob\n", "gitlab").owners_for(["a"]) == ["@bob"]
