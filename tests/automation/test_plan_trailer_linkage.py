"""patch mode (F4): pull requests opened from any branch are linked by the GreenKube-Plan trailer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from greenkube.automation.delivery import (
    PlanTrailerLinker,
    plan_ids_in_commit_messages,
    plan_ids_in_pull_request_body,
)
from greenkube.automation.git.base import GitRepository, PullRequestInfo
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]

REPO = GitRepository(provider="github", owner="acme", name="gitops")


def _pr(number: int, *, body: str = "", head: str = "fix/rightsize", sha: str | None = None) -> PullRequestInfo:
    return PullRequestInfo(
        number=number,
        url=f"https://github.com/acme/gitops/pull/{number}",
        status="open",
        raw={"body": body, "head": {"ref": head, "sha": sha or f"{number:040d}"}, "base": {"ref": "main"}},
    )


class FakeProvider:
    def __init__(self, pull_requests: list[PullRequestInfo], commits: dict[int, list[str]]) -> None:
        self.pull_requests = pull_requests
        self.commits = commits
        self.commit_reads: list[int] = []
        self.listed: list[str] = []

    async def list_open_pull_requests(self, repository, base):
        self.listed.append(base)
        return list(self.pull_requests)

    async def list_pull_request_commit_messages(self, repository, number):
        self.commit_reads.append(number)
        return self.commits.get(number, [])

    async def close(self) -> None:
        return None


class FakeWorker:
    def __init__(self, plans, provider: FakeProvider, *, refuse: set[int] | None = None) -> None:
        self.plans = SimpleNamespace(list_plans=self._list)
        self._plans = plans
        self.provider_resolver = SimpleNamespace(resolve=self._resolve)
        self.provider = provider
        self.refuse = refuse or set()
        self.reports: list[tuple[str, str, str | None]] = []

    async def _list(self, *, status=None):
        return list(self._plans)

    async def _resolve(self, source):
        return SimpleNamespace(provider=self.provider, repository=REPO)

    async def record_external_delivery(self, plan_id, *, pr_url, head_sha, actor):
        number = int(pr_url.rsplit("/", 1)[-1])
        self.reports.append((plan_id, pr_url, head_sha))
        if number in self.refuse:
            raise RuntimeError("delivery_not_verified")
        return SimpleNamespace(plan_id=plan_id)


def _patch_plan(plan_id: str = "pln-1"):
    plan = fx.plan(plan_id=plan_id)
    return plan.model_copy(update={"delivery_mode": "patch", "digest": "sha256:" + "d" * 64})


def test_body_and_commit_trailers_name_plans():
    body = "Rightsize api\n\nGreenKube-Plan: pln-7\n"
    assert plan_ids_in_pull_request_body(body) == {"pln-7"}
    assert plan_ids_in_pull_request_body("no trailer") == set()
    assert plan_ids_in_commit_messages(["fix", "greenkube: rightsize\n\nGreenKube-Plan: pln-9"]) == {"pln-9"}


@pytest.mark.asyncio
async def test_a_pull_request_from_another_branch_is_linked_by_its_body_trailer():
    provider = FakeProvider([_pr(3), _pr(4, body="Applied with git am\n\nGreenKube-Plan: pln-1")], {})
    worker = FakeWorker([_patch_plan()], provider)

    assert await PlanTrailerLinker(worker).link_once() == 1

    assert worker.reports == [("pln-1", "https://github.com/acme/gitops/pull/4", f"{4:040d}")]
    assert provider.listed == ["main"]


@pytest.mark.asyncio
async def test_a_pull_request_is_linked_by_the_trailer_of_its_commits():
    """Regression: only the plan branch and the PR body were considered."""
    provider = FakeProvider(
        [_pr(5, body="Rightsizing from the GreenKube patch")],
        {5: ["greenkube: rightsize payments/api\n\nGreenKube-Plan: pln-1\nGreenKube-Recommendations: 41,42"]},
    )
    worker = FakeWorker([_patch_plan()], provider)

    assert await PlanTrailerLinker(worker).link_once() == 1
    assert worker.reports[0][:2] == ("pln-1", "https://github.com/acme/gitops/pull/5")


@pytest.mark.asyncio
async def test_refused_or_unrelated_pull_requests_are_not_retried_until_their_head_changes():
    provider = FakeProvider([_pr(6), _pr(7, body="GreenKube-Plan: pln-1")], {6: ["unrelated"]})
    worker = FakeWorker([_patch_plan()], provider, refuse={7})
    linker = PlanTrailerLinker(worker)

    assert await linker.link_once() == 0
    assert await linker.link_once() == 0

    assert provider.commit_reads == [6]
    assert len(worker.reports) == 1


@pytest.mark.asyncio
async def test_only_approved_patch_plans_are_considered():
    plan = fx.plan().model_copy(update={"delivery_mode": "ci", "digest": "sha256:" + "d" * 64})
    provider = FakeProvider([_pr(8, body="GreenKube-Plan: pln-1")], {})
    worker = FakeWorker([plan], provider)

    assert await PlanTrailerLinker(worker).link_once() == 0
    assert provider.listed == []


@pytest.mark.asyncio
@respx.mock
async def test_github_lists_open_pull_requests_into_the_base_and_their_commit_messages():
    api = "https://api.github.com/repos/acme/gitops"
    pulls = respx.get(f"{api}/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 4,
                    "html_url": "https://github.com/acme/gitops/pull/4",
                    "state": "open",
                    "head": {"ref": "fix", "sha": "a" * 40},
                    "base": {"ref": "main"},
                },
                {
                    "number": 5,
                    "html_url": "https://github.com/acme/gitops/pull/5",
                    "state": "open",
                    "head": {"ref": "x", "sha": "b" * 40},
                    "base": {"ref": "release"},
                },
            ],
        )
    )
    respx.get(f"{api}/pulls/4/commits").mock(
        return_value=httpx.Response(200, json=[{"sha": "a" * 40, "commit": {"message": "m\n\nGreenKube-Plan: p"}}])
    )
    provider = GitHubProvider("token")
    try:
        listed = await provider.list_open_pull_requests(REPO, "main")
        messages = await provider.list_pull_request_commit_messages(REPO, 4)
    finally:
        await provider.close()

    assert [item.number for item in listed] == [4]
    assert dict(pulls.calls[0].request.url.params)["base"] == "main"
    assert messages == ["m\n\nGreenKube-Plan: p"]


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_reads_merge_request_commit_messages():
    repo = GitRepository(provider="gitlab", owner="acme", name="gitops")
    respx.get(url__regex=r".*/merge_requests/3/commits.*").mock(
        return_value=httpx.Response(200, json=[{"id": "c" * 40, "message": "x\n\nGreenKube-Plan: pln-3"}])
    )
    provider = GitLabProvider("token")
    try:
        messages: Any = await provider.list_pull_request_commit_messages(repo, 3)
    finally:
        await provider.close()
    assert plan_ids_in_commit_messages(messages) == {"pln-3"}
