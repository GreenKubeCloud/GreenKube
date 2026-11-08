"""The plan worker opens readable, labelled, routed and validated PRs (roadmap J1, J2, J3, J5).

Real adapters run against the counting in-memory Git servers.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx

from greenkube.automation.pr_content import PullRequestRenderSettings, parse_footer
from greenkube.automation.pr_content.model import PullRequestFeatures
from greenkube.gitops.contracts import PlanStatus
from greenkube.models.metrics import RecommendationRecord
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]
from tests.automation.fake_git_servers import FakeGitRepository  # pyrefly: ignore[missing-import]
from tests.automation.test_plan_worker_provider_calls import (  # pyrefly: ignore[missing-import]
    BASE_MANIFEST,
    MANIFEST_PATH,
    SERVERS,
    _plan,
    _worker,
)


class Recommendations:
    def __init__(self, *records: RecommendationRecord) -> None:
        self.records = {record.id: record for record in records}

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
        return self.records.get(rec_id)


def _run(provider: str, *, risk: str = "low", codeowners: str | None = None, features: Any = None):
    files = {MANIFEST_PATH: BASE_MANIFEST}
    if codeowners is not None:
        path = {"github": ".github/CODEOWNERS", "gitlab": "CODEOWNERS", "gitea": "CODEOWNERS"}[provider]
        files[path] = codeowners
    repository = FakeGitRepository.with_files(files)
    server = SERVERS[provider](repository)
    server.users["alice"] = 42
    plan = _plan(provider, repository.refs["main"])
    worker, operations, plans, pull_requests = _worker(provider, plan)
    worker.recommendation_repository = Recommendations(fx.recommendation(41, risk_level=risk))
    worker._render_settings = PullRequestRenderSettings(external_url="https://greenkube.example.com")
    if features is not None:
        worker._pull_request_features = features
    return worker, operations, plans, server, repository, plan


async def _process(worker, server) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await worker.process_once("worker-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(SERVERS))
async def test_pr_gets_readable_title_footer_labels_codeowners_and_validation_status(provider):
    codeowners = {
        "github": "apps/payments/ @alice @acme/payments\n",
        "gitlab": "[Payments]\napps/payments/ @alice\n",
        "gitea": "apps/payments/.* @alice @acme/payments\n",
    }[provider]
    worker, operations, plans, server, repository, plan = _run(provider, codeowners=codeowners)

    await _process(worker, server)

    assert operations.failures == []
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    request = server.pull_requests[0]
    assert server.pull_request_labels[1] == ["greenkube", "optimization/cpu-rightsizing", "risk/low"]
    if provider == "gitlab":
        assert request["reviewers"] == [{"id": 42}]
    else:
        assert server.requested_reviewers[1] == ["alice", "payments"]
    head = repository.refs[[ref for ref in repository.refs if ref.startswith("greenkube/plan/")][0]]
    assert [(status["sha"], status["state"]) for status in server.statuses] == [(head, "success")]
    commit_message = repository.commits[head].message
    assert commit_message.endswith("GreenKube-Plan: plan-1\nGreenKube-Recommendations: 41\n")
    assert commit_message.startswith("greenkube: rightsize payments/api · −$300/yr")


@pytest.mark.asyncio
async def test_github_body_and_title_reach_the_provider():
    worker, _, _, server, _, _ = _run("github")
    bodies: list[dict] = []
    original = server.route

    def capture(method, path, query, body):
        if method == "POST" and path.endswith("/pulls"):
            bodies.append(body)
        return original(method, path, query, body)

    server.route = capture
    await _process(worker, server)
    assert bodies[0]["title"] == "greenkube: rightsize payments/api · −$300/yr · −12 kgCO₂e/yr"
    assert bodies[0]["draft"] is False
    footer = parse_footer(bodies[0]["body"])
    assert footer is not None and footer["plan_id"] == "plan-1" and footer["recommendation_ids"] == [41]
    assert "[#41](https://greenkube.example.com/recommendations/41)" in bodies[0]["body"]


@pytest.mark.asyncio
async def test_high_risk_github_plans_open_as_draft_pull_requests():
    worker, operations, _, server, _, _ = _run("github", risk="high")
    await _process(worker, server)
    assert operations.failures == []
    assert server.pull_requests[0]["draft"] is True
    assert server.pull_request_labels[1][-1] == "risk/high"


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "prefix"), [("gitlab", "Draft: "), ("gitea", "WIP: ")])
async def test_high_risk_gitlab_and_gitea_requests_get_a_draft_title(provider, prefix):
    worker, operations, _, server, _, _ = _run(provider, risk="high")
    titles: list[str] = []
    original = server.route

    def capture(method, path, query, body):
        if method == "POST" and (path.endswith("/pulls") or path.endswith("/merge_requests")):
            titles.append(body["title"])
        return original(method, path, query, body)

    server.route = capture
    await _process(worker, server)
    assert operations.failures == []
    assert titles == [f"{prefix}greenkube: rightsize payments/api · −$300/yr · −12 kgCO₂e/yr"]
    assert server.pull_request_labels[1][-1] == "risk/high"


@pytest.mark.asyncio
async def test_a_refused_draft_is_reopened_ready_for_review():
    worker, operations, plans, server, _, _ = _run("github", risk="high")
    original = server.route
    attempts: list[bool] = []

    def refuse_drafts(method, path, query, body):
        if method == "POST" and path.endswith("/pulls"):
            attempts.append(bool(body.get("draft")))
            if body.get("draft"):
                from httpx import Response

                return Response(422, json={"message": "Draft pull requests are not supported in this repository."})
        return original(method, path, query, body)

    server.route = refuse_drafts
    await _process(worker, server)
    assert attempts == [True, False]
    assert operations.failures == []
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN


@pytest.mark.asyncio
async def test_metadata_failures_never_fail_an_opened_pull_request():
    worker, operations, plans, server, _, _ = _run("github")
    original = server.route

    def broken_metadata(method, path, query, body):
        if "/labels" in path or "/statuses/" in path:
            from httpx import Response

            return Response(403, json={"message": "Resource not accessible by integration"})
        return original(method, path, query, body)

    server.route = broken_metadata
    await _process(worker, server)
    assert operations.failures == []
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert len(server.pull_requests) == 1


@pytest.mark.asyncio
async def test_features_can_be_switched_off():
    worker, _, _, server, _, _ = _run(
        "github",
        risk="high",
        codeowners="* @alice\n",
        features=PullRequestFeatures(
            default_labels=False, draft_high_risk=False, codeowners=False, commit_status=False
        ),
    )
    await _process(worker, server)
    assert server.pull_requests[0]["draft"] is False
    assert server.pull_request_labels == {}
    assert server.requested_reviewers == {}
    assert server.statuses == []
    assert not any("CODEOWNERS" in request for request in server.requests)


@pytest.mark.asyncio
async def test_adopted_draft_request_is_not_marked_ready_for_review():
    worker, operations, plans, server, repository, plan = _run("github", risk="high")
    await _process(worker, server)
    assert server.pull_requests[0]["draft"] is True

    # A reclaimed operation re-applies options to the open request: it must stay a draft.
    from tests.automation.test_plan_worker_provider_calls import Operations  # pyrefly: ignore[missing-import]

    worker.operations = Operations(plans.plans["plan-1"])
    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"status": PlanStatus.PR_OPEN})
    await _process(worker, server)
    assert not any(request.endswith("/ready_for_review") for request in server.requests)
    assert operations.failures == []
