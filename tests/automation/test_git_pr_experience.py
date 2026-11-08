"""Provider adapters: labels, reviewers, commit statuses, comments and CODEOWNERS (J2, J3, J5).

The real GitHub, GitLab and Gitea adapters run against the in-memory servers of
``fake_git_servers``, which count every request.
"""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import (
    CommentUpsertResult,
    GitProviderCapability,
    GitProviderError,
    PullRequestInfo,
    PullRequestOptions,
)
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider
from tests.automation.fake_git_servers import (  # pyrefly: ignore[missing-import]
    BOT_USER_ID,
    FakeGiteaServer,
    FakeGitHubServer,
    FakeGitLabServer,
    FakeGitRepository,
)

PROVIDERS = {
    "github": (GitHubProvider, FakeGitHubServer, None, "https://github.com/acme/gitops.git"),
    "gitlab": (GitLabProvider, FakeGitLabServer, None, "https://gitlab.com/acme/gitops.git"),
    "gitea": (GiteaProvider, FakeGiteaServer, "http://gitea.local", "http://gitea.local/acme/gitops.git"),
}
MARKER = "<!-- greenkube:lifecycle v1 -->"


def _setup(name: str, files: dict[str, str] | None = None):
    adapter, server_type, api_base_url, url = PROVIDERS[name]
    repository = FakeGitRepository.with_files(files or {"apps/payments/deployment.yaml": "kind: Deployment\n"})
    server = server_type(repository)
    provider = adapter("token", api_base_url=api_base_url)
    return provider, server, provider.repository(url, "main"), repository


def _pull_request(number: int = 1, raw: dict | None = None) -> PullRequestInfo:
    return PullRequestInfo(number=number, url=f"https://example.test/pr/{number}", status="open", raw=raw or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_lifecycle_comment_is_created_once_then_edited_never_duplicated(name):
    provider, server, repo, _ = _setup(name)
    server.add_comment(7, 1, "LGTM from a human", author_id=77)
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        first = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\nsynced")
        calls_after_create = server.calls()
        second = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\nsynced")
        calls_after_unchanged = server.calls()
        third = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\napplied")

    assert first[0] is CommentUpsertResult.CREATED
    assert second == (CommentUpsertResult.UNCHANGED, first[1])
    assert third == (CommentUpsertResult.UPDATED, first[1])
    # identity + list + create, then identity + list only, then identity + list + edit.
    assert calls_after_create == 3
    assert calls_after_unchanged - calls_after_create == 2
    assert server.calls() - calls_after_unchanged == 3
    greenkube_comments = [item for item in server.comments.values() if item["body"].startswith(MARKER)]
    assert [item["body"] for item in greenkube_comments] == [f"{MARKER}\napplied"]
    assert server.comments[7]["body"] == "LGTM from a human"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_marker_comment_written_by_someone_else_is_never_adopted(name):
    """A user who posts a comment starting with the marker must not get GreenKube's status written into it."""
    provider, server, repo, _ = _setup(name)
    spoof = f"{MARKER}\nOutcome: Realized (trust me)"
    server.add_comment(7, 1, spoof, author_id=77)
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        created = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\nsynced")
        edited = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\napplied")

    assert created[0] is CommentUpsertResult.CREATED
    assert created[1] != 7
    assert edited == (CommentUpsertResult.UPDATED, created[1])
    assert server.comments[7]["body"] == spoof
    assert server.comments[created[1]] == {
        "id": created[1],
        "number": 1,
        "body": f"{MARKER}\napplied",
        "author_id": BOT_USER_ID,
    }
    assert not any(request.startswith(("PATCH", "PUT")) and request.endswith("/7") for request in server.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_pull_request_author_identifies_greenkube_when_the_token_cannot_read_its_user(name):
    provider, server, repo, repository = _setup(name)
    repository.refs["greenkube/plan-1"] = repository.refs["main"]
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.create_pull_request(
            repo,
            head="greenkube/plan-1",
            base="main",
            title="greenkube: plan",
            body="body",
            options=provider.validate_pull_request_options(PullRequestOptions()),
        )
        server.identity_status = 403
        server.add_comment(7, 1, f"{MARKER}\nforeign", author_id=77)
        server.add_comment(8, 1, f"{MARKER}\nmine", author_id=BOT_USER_ID)
        result = await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\nmeasuring")

    assert result == (CommentUpsertResult.UPDATED, 8)
    assert server.comments[7]["body"] == f"{MARKER}\nforeign"


@pytest.mark.asyncio
async def test_lifecycle_comment_is_refused_when_greenkube_identity_is_unknown():
    provider, server, repo, _ = _setup("github")
    server.identity_status = 403
    server.pull_requests.append({"number": 1, "html_url": "https://github.com/acme/gitops/pull/1", "state": "open"})
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        with pytest.raises(GitProviderError, match="identity"):
            await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body=f"{MARKER}\nsynced")
    assert not server.comments


@pytest.mark.asyncio
async def test_comment_must_start_with_its_marker():
    provider, _, repo, _ = _setup("github")
    with pytest.raises(GitProviderError, match="marker"):
        await provider.upsert_pull_request_comment(repo, 1, marker=MARKER, body="no marker")


@pytest.mark.asyncio
@respx.mock
async def test_github_comment_lookup_follows_pagination():
    provider, _, repo, _ = _setup("github")
    api = "https://api.github.com/repos/acme/gitops"
    respx.get("https://api.github.com/user").mock(return_value=Response(200, json={"id": 9, "login": "bot"}))
    first_page = [{"id": index, "body": "noise", "user": {"id": 9}} for index in range(1, 101)]
    respx.get(f"{api}/issues/3/comments", params={"per_page": "100", "page": "1"}).mock(
        return_value=Response(200, json=first_page, headers={"Link": f'<{api}/issues/3/comments?page=2>; rel="next"'})
    )
    respx.get(f"{api}/issues/3/comments", params={"per_page": "100", "page": "2"}).mock(
        return_value=Response(200, json=[{"id": 555, "body": f"{MARKER}\nold", "user": {"id": 9}}])
    )
    edit = respx.patch(f"{api}/issues/comments/555").mock(return_value=Response(200, json={"id": 555}))

    result = await provider.upsert_pull_request_comment(repo, 3, marker=MARKER, body=f"{MARKER}\nnew")

    assert result == (CommentUpsertResult.UPDATED, 555)
    assert json.loads(edit.calls[0].request.content) == {"body": f"{MARKER}\nnew"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_validation_commit_status_targets_the_head_commit(name):
    provider, server, repo, repository = _setup(name)
    head = repository.refs["main"]
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.set_commit_status(
            repo,
            head,
            state="success",
            context="greenkube/validation",
            description="GreenKube: render diff valid; policy passed.",
            target_url="https://greenkube.example.com/changes/pln-1",
        )
    assert provider.supports(GitProviderCapability.COMMIT_STATUS)
    assert len(server.statuses) == 1
    status = server.statuses[0]
    assert status["sha"] == head
    assert status.get("context", status.get("name")) == "greenkube/validation"
    assert status["state"] == "success"
    assert status["target_url"] == "https://greenkube.example.com/changes/pln-1"


@pytest.mark.asyncio
async def test_gitlab_maps_failure_to_its_failed_state_and_status_requires_a_full_sha():
    provider, server, repo, repository = _setup("gitlab")
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.set_commit_status(
            repo, repository.refs["main"], state="failure", context="greenkube/validation", description="x"
        )
        with pytest.raises(GitProviderError):
            await provider.set_commit_status(
                repo, "main", state="success", context="greenkube/validation", description="x"
            )
    assert server.statuses[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_github_adds_labels_and_only_missing_reviewers():
    provider, server, repo, _ = _setup("github")
    server.requested_reviewers[1] = ["alice"]
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.add_pull_request_metadata(
            repo,
            _pull_request(),
            labels=("greenkube", "risk/low"),
            reviewers=("alice", "bob"),
            team_reviewers=("payments",),
        )
    assert server.pull_request_labels[1] == ["greenkube", "risk/low"]
    assert server.requested_reviewers[1] == ["alice", "bob", "payments"]


@pytest.mark.asyncio
async def test_gitlab_adds_labels_and_merges_reviewer_ids():
    provider, server, repo, repository = _setup("gitlab")
    server.pull_requests.append({"iid": 1, "state": "opened", "reviewers": [{"id": 5}]})
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.add_pull_request_metadata(
            repo, _pull_request(raw={"reviewers": [{"id": 5}]}), labels=("greenkube", "risk/high"), reviewers=("9",)
        )
    assert server.pull_request_labels[1] == ["greenkube", "risk/high"]
    assert server.pull_requests[0]["reviewers"] == [{"id": 5}, {"id": 9}]
    with pytest.raises(GitProviderError, match="numeric"):
        await provider.add_pull_request_metadata(repo, _pull_request(), reviewers=("bob",))


@pytest.mark.asyncio
async def test_gitea_creates_missing_labels_instead_of_failing():
    provider, server, repo, _ = _setup("gitea")
    server.labels["greenkube"] = 13
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        await provider.add_pull_request_metadata(
            repo, _pull_request(), labels=("greenkube", "risk/low"), reviewers=("alice",), team_reviewers=("ops",)
        )
    assert set(server.labels) == {"greenkube", "risk/low"}
    assert server.pull_request_labels[1] == ["greenkube", "risk/low"]
    assert server.requested_reviewers[1] == ["alice", "ops"]
    assert sum(1 for request in server.requests if request.startswith("POST") and request.endswith("/labels")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "path", "content", "expected"),
    [
        (
            "github",
            ".github/CODEOWNERS",
            "apps/payments/ @alice @acme/payments @other-org/team\n",
            (["alice"], ["payments"]),
        ),
        ("gitlab", "CODEOWNERS", "[Payments]\napps/payments/ @alice @acme/group\n", (["42"], [])),
        ("gitea", ".gitea/CODEOWNERS", "apps/payments/.* @alice @acme/payments\n", (["alice"], ["payments"])),
    ],
)
async def test_codeowners_are_read_from_the_revision_and_mapped_to_reviewers(name, path, content, expected):
    provider, server, repo, repository = _setup(
        name, {"apps/payments/deployment.yaml": "kind: Deployment\n", path: content}
    )
    server.users["alice"] = 42
    revision = repository.refs["main"]
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        codeowners = await provider.read_codeowners(repo, revision)
        assert codeowners is not None
        owners = codeowners.owners_for(["apps/payments/deployment.yaml"])
        reviewers = await provider.codeowner_reviewers(repo, owners)
    assert reviewers == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_missing_codeowners_returns_none(name):
    provider, server, repo, repository = _setup(name)
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await provider.read_codeowners(repo, repository.refs["main"]) is None
    # A tree-backed adapter answers from one tree read; the others probe each documented path.
    assert server.calls() == (1 if name == "github" else 3)
