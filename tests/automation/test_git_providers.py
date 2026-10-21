# tests/automation/test_git_providers.py
"""Tests for the GitHub, GitLab and Gitea provider adapters (respx-mocked)."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import AsyncByteStream, ConnectError, Response

from greenkube.automation.git.base import (
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFileEdit,
    GitProviderArchiveLimitError,
    GitProviderCapability,
    GitProviderCapabilityError,
    GitProviderError,
    GitProviderTransientError,
    GitRepository,
    PullRequestInfo,
    PullRequestOptions,
    parse_repo_url,
    validate_git_ref,
)
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider


def test_repository_validation_requires_explicit_custom_provider_opt_in():
    with pytest.raises(GitProviderError, match="Unsupported Git provider"):
        GitRepository(provider="fake", owner="acme", name="manifests")

    repository = GitRepository(
        provider="fake",
        owner="acme",
        name="manifests",
        validate_provider=False,
    )
    assert repository.full_name == "acme/manifests"


class TestParseRepoUrl:
    def test_github_https(self):
        assert parse_repo_url("https://github.com/acme/manifests.git") == ("acme", "manifests")

    def test_github_ssh(self):
        assert parse_repo_url("git@github.com:acme/manifests.git") == ("acme", "manifests")

    def test_gitlab_nested_group(self):
        assert parse_repo_url("https://gitlab.com/group/subgroup/manifests.git", "gitlab") == (
            "group/subgroup",
            "manifests",
        )

    def test_https_with_credentials(self):
        assert parse_repo_url("https://user:token@github.com/acme/manifests") == ("acme", "manifests")

    def test_invalid_url_raises(self):
        with pytest.raises(GitProviderError):
            parse_repo_url("not-a-url")


@pytest.mark.asyncio
@respx.mock
async def test_github_full_flow():
    respx.get("https://api.github.com/repos/acme/manifests").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    content = "kind: Deployment\nmetadata:\n  name: api\n"
    respx.get("https://api.github.com/repos/acme/manifests/contents/apps/api.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(content.encode()).decode(), "sha": "abc123"},
        )
    )
    respx.get("https://api.github.com/repos/acme/manifests/git/ref/heads/main").mock(
        return_value=Response(200, json={"object": {"sha": "base-sha"}})
    )
    respx.get("https://api.github.com/repos/acme/manifests/git/ref/heads/greenkube/reco-1").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    respx.post("https://api.github.com/repos/acme/manifests/git/refs").mock(
        return_value=Response(201, json={"ref": "refs/heads/greenkube/reco-1"})
    )
    respx.put("https://api.github.com/repos/acme/manifests/contents/apps/api.yaml").mock(
        return_value=Response(200, json={"content": {"sha": "new"}})
    )
    respx.post("https://api.github.com/repos/acme/manifests/pulls").mock(
        return_value=Response(
            201,
            json={"number": 7, "html_url": "https://github.com/acme/manifests/pull/7"},
        )
    )

    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    assert await provider.get_default_branch(repo) == "main"
    git_file = await provider.get_file(repo, "apps/api.yaml", "main")
    assert (
        git_file is not None
        and git_file.sha == "abc123"
        and git_file.blob_sha == "abc123"
        and git_file.content == content
    )
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", content, "msg", "greenkube/reco-1", sha="abc123")
    pr = await provider.create_pull_request(repo, head="greenkube/reco-1", base="main", title="t", body="b")
    assert isinstance(pr, PullRequestInfo)
    assert pr["number"] == 7
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_archive_uses_full_commit_and_drops_auth_on_codeload_redirect(monkeypatch):
    revision = "a" * 40
    archive = b"bounded-archive-content"
    api = f"https://api.github.com/repos/acme/manifests/tarball/{revision}"
    codeload = f"https://codeload.github.com/acme/manifests/legacy.tar.gz/{revision}"
    api_route = respx.get(api).mock(return_value=Response(302, headers={"Location": codeload}))
    archive_route = respx.get(codeload).mock(return_value=Response(200, content=archive))
    provider = GitHubProvider("private-provider-token")
    monkeypatch.setattr(
        provider,
        "_headers",
        lambda: {"Authorization": "Bearer private-provider-token", "Accept": "application/octet-stream"},
    )
    repository = GitRepository(provider="github", owner="acme", name="manifests")

    assert await provider.get_archive(repository, revision, max_bytes=len(archive)) == archive

    assert api_route.calls[0].request.url.path.endswith(f"/tarball/{revision}")
    assert api_route.calls[0].request.headers["authorization"] == "Bearer private-provider-token"
    assert "authorization" not in archive_route.calls[0].request.headers
    assert provider.supports(GitProviderCapability.REPOSITORY_ARCHIVE)
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["gitlab", "gitea"])
@respx.mock
async def test_gitlab_and_gitea_archives_are_requested_at_the_immutable_commit(provider_type):
    revision = "b" * 40
    archive = b"archive"
    if provider_type == "gitlab":
        api_base = "https://gitlab.test/api/v4"
        provider = GitLabProvider("gitlab-private-token", api_base_url="https://gitlab.test")
        repository = GitRepository(provider="gitlab", owner="acme", name="manifests", base_url=api_base)
        route = respx.get(
            f"{api_base}/projects/acme%2Fmanifests/repository/archive.tar.gz",
            params={"sha": revision},
        ).mock(return_value=Response(200, content=archive))
    else:
        api_base = "http://gitea.test/api/v1"
        provider = GiteaProvider("gitea-private-token", api_base_url=api_base)
        repository = GitRepository(provider="gitea", owner="acme", name="manifests", base_url=api_base)
        route = respx.get(f"{api_base}/repos/acme/manifests/archive/{revision}.tar.gz").mock(
            return_value=Response(200, content=archive)
        )

    assert await provider.get_archive(repository, revision, max_bytes=len(archive)) == archive

    request = route.calls[0].request
    if provider_type == "gitlab":
        assert request.url.params["sha"] == revision
        assert request.headers["private-token"] == "gitlab-private-token"
    else:
        assert request.url.path.endswith(f"/archive/{revision}.tar.gz")
        assert request.headers["authorization"] == "token gitea-private-token"
    assert provider.supports(GitProviderCapability.REPOSITORY_ARCHIVE)
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_repository_archive_is_stream_bounded_and_rejects_non_commit_refs():
    revision = "c" * 40
    api = f"http://gitea.test/api/v1/repos/acme/manifests/archive/{revision}.tar.gz"

    class ChunkedArchive(AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

        async def aclose(self) -> None:
            return None

    route = respx.get(api).mock(return_value=Response(200, stream=ChunkedArchive()))
    provider = GiteaProvider("token", api_base_url="http://gitea.test")
    repository = GitRepository(provider="gitea", owner="acme", name="manifests")

    with pytest.raises(GitProviderArchiveLimitError, match="compressed-byte limit"):
        await provider.get_archive(repository, revision, max_bytes=4)

    with pytest.raises(GitProviderError, match="full commit SHA"):
        await provider.get_archive(repository, "main", max_bytes=100)

    assert route.call_count == 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_archive_rejects_untrusted_cross_origin_redirect():
    revision = "d" * 40
    api = f"https://api.github.com/repos/acme/manifests/tarball/{revision}"
    external = "https://attacker.example/archive.tar.gz"
    route = respx.get(api).mock(return_value=Response(302, headers={"Location": external}))
    provider = GitHubProvider("token")
    repository = GitRepository(provider="github", owner="acme", name="manifests")

    with pytest.raises(GitProviderError, match="unsupported archive redirect"):
        await provider.get_archive(repository, revision, max_bytes=1024)

    assert route.called
    assert len(respx.calls) == 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_create_pull_request_applies_draft_labels_and_missing_reviewers():
    api = "https://api.github.com/repos/acme/manifests"
    create_route = respx.post(f"{api}/pulls").mock(
        return_value=Response(
            201,
            json={"number": 8, "html_url": "https://github.com/acme/manifests/pull/8", "draft": True},
        )
    )
    labels_route = respx.post(f"{api}/issues/8/labels").mock(return_value=Response(200, json=[]))
    reviewers_get_route = respx.get(f"{api}/pulls/8/requested_reviewers").mock(
        return_value=Response(
            200,
            json={
                "users": [{"login": "already-requested"}],
                "teams": [{"slug": "existing-team"}],
            },
        )
    )
    reviewers_post_route = respx.post(f"{api}/pulls/8/requested_reviewers").mock(return_value=Response(201, json={}))
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")
    options = PullRequestOptions(
        draft=True,
        labels=("greenkube", "sustainability"),
        reviewers=("already-requested", "new-reviewer"),
        team_reviewers=("existing-team", "platform"),
    )

    pull_request = await provider.create_pull_request(
        repo,
        head="greenkube/plan-8",
        base="main",
        title="Tune workload",
        body="Rich plan body",
        options=options,
    )

    create_body = json.loads(create_route.calls[0].request.content)
    assert create_body == {
        "title": "Tune workload",
        "head": "greenkube/plan-8",
        "base": "main",
        "body": "Rich plan body",
        "draft": True,
    }
    assert json.loads(labels_route.calls[0].request.content) == {"labels": ["greenkube", "sustainability"]}
    assert reviewers_get_route.called
    assert json.loads(reviewers_post_route.calls[0].request.content) == {
        "reviewers": ["new-reviewer"],
        "team_reviewers": ["platform"],
    }
    assert pull_request.number == 8
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_error_is_wrapped():
    respx.get("https://api.github.com/repos/acme/manifests").mock(
        return_value=Response(401, json={"message": "Bad credentials"})
    )
    provider = GitHubProvider("bad")
    repo = provider.repository("https://github.com/acme/manifests.git")
    with pytest.raises(GitProviderError) as exc:
        await provider.get_default_branch(repo)
    assert "Bad credentials" in str(exc.value)
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_flow_maps_merge_request():
    project = "group%2Fsubgroup%2Fmanifests"
    respx.get(f"https://gitlab.com/api/v4/projects/{project}").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx.get(f"https://gitlab.com/api/v4/projects/{project}/repository/files/apps%2Fapi.yaml").mock(
        return_value=Response(
            200,
            json={
                "content": base64.b64encode(b"kind: Deployment\n").decode(),
                "last_commit_id": "sha1",
                "blob_id": "blob-sha1",
            },
        )
    )
    respx.post(f"https://gitlab.com/api/v4/projects/{project}/repository/branches").mock(
        return_value=Response(201, json={"name": "greenkube/reco-1"})
    )
    respx.put(f"https://gitlab.com/api/v4/projects/{project}/repository/files/apps%2Fapi.yaml").mock(
        return_value=Response(200, json={"file_path": "apps/api.yaml"})
    )
    respx.post(f"https://gitlab.com/api/v4/projects/{project}/merge_requests").mock(
        return_value=Response(201, json={"iid": 3, "web_url": "https://gitlab.com/x/-/mr/3", "state": "opened"})
    )

    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/group/subgroup/manifests.git")

    assert await provider.get_default_branch(repo) == "main"
    git_file = await provider.get_file(repo, "apps/api.yaml", "main")
    assert git_file is not None and git_file.sha == "sha1" and git_file.blob_sha == "blob-sha1"
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", "content", "msg", "greenkube/reco-1")
    pr = await provider.create_pull_request(repo, head="h", base="main", title="t", body="b")
    assert isinstance(pr, PullRequestInfo)
    assert pr["number"] == 3
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_create_merge_request_maps_draft_labels_and_reviewer_ids():
    endpoint = "https://gitlab.com/api/v4/projects/acme%2Fmanifests/merge_requests"
    create_route = respx.post(endpoint).mock(
        return_value=Response(
            201,
            json={"iid": 4, "web_url": "https://gitlab.com/acme/manifests/-/merge_requests/4", "state": "opened"},
        )
    )
    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/acme/manifests.git")

    pull_request = await provider.create_pull_request(
        repo,
        head="greenkube/plan-4",
        base="main",
        title="Tune workload",
        body="Rich plan body",
        options=PullRequestOptions(
            draft=True,
            labels=("greenkube", "sustainability"),
            reviewers=("17", "29"),
        ),
    )

    assert json.loads(create_route.calls[0].request.content) == {
        "source_branch": "greenkube/plan-4",
        "target_branch": "main",
        "title": "Draft: Tune workload",
        "description": "Rich plan body",
        "labels": "greenkube,sustainability",
        "reviewer_ids": [17, 29],
    }
    assert pull_request.number == 4
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_rejects_unsupported_or_malformed_review_options_before_pr_creation():
    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/acme/manifests.git")

    with pytest.raises(GitProviderCapabilityError, match="team_reviewers"):
        await provider.create_pull_request(
            repo,
            head="greenkube/plan-5",
            base="main",
            title="Tune workload",
            body="Rich plan body",
            options=PullRequestOptions(team_reviewers=("platform",)),
        )
    with pytest.raises(GitProviderError, match="numeric user IDs"):
        await provider.create_pull_request(
            repo,
            head="greenkube/plan-5",
            base="main",
            title="Tune workload",
            body="Rich plan body",
            options=PullRequestOptions(reviewers=("octocat",)),
        )

    assert len(respx.calls) == 0
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_flow():
    respx.get("http://gitea.local/api/v1/repos/acme/manifests").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    content = "kind: Deployment\n"
    respx.get("http://gitea.local/api/v1/repos/acme/manifests/contents/apps%2Fapi.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(content.encode()).decode(), "sha": "abc"},
        )
    )
    respx.get("http://gitea.local/api/v1/repos/acme/manifests/branches/greenkube/reco-1").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    respx.post("http://gitea.local/api/v1/repos/acme/manifests/branches").mock(
        return_value=Response(201, json={"name": "greenkube/reco-1"})
    )
    respx.put("http://gitea.local/api/v1/repos/acme/manifests/contents/apps%2Fapi.yaml").mock(
        return_value=Response(200, json={"content": {"sha": "new"}})
    )
    respx.post("http://gitea.local/api/v1/repos/acme/manifests/pulls").mock(
        return_value=Response(
            201, json={"number": 11, "html_url": "http://gitea.local/acme/manifests/pulls/11", "state": "open"}
        )
    )

    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    assert await provider.get_default_branch(repo) == "main"
    git_file = await provider.get_file(repo, "apps/api.yaml", "main")
    assert git_file is not None and git_file.sha == "abc" and git_file.blob_sha == "abc"
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", content, "msg", "greenkube/reco-1", sha="abc")
    pr = await provider.create_pull_request(repo, head="h", base="main", title="t", body="b")
    assert isinstance(pr, PullRequestInfo)
    assert pr["number"] == 11
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_create_pull_request_resolves_labels_and_includes_reviewer_requests():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    labels_route = respx.get(f"{api}/labels").mock(
        return_value=Response(
            200,
            json=[
                {"id": 13, "name": "greenkube"},
                {"id": 17, "name": "sustainability"},
            ],
        )
    )
    create_route = respx.post(f"{api}/pulls").mock(
        return_value=Response(
            201,
            json={"number": 12, "html_url": "http://gitea.local/acme/manifests/pulls/12", "state": "open"},
        )
    )
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    pull_request = await provider.create_pull_request(
        repo,
        head="greenkube/plan-12",
        base="main",
        title="Tune workload",
        body="Rich plan body",
        options=PullRequestOptions(
            labels=("greenkube", "sustainability"),
            reviewers=("octocat",),
            team_reviewers=("platform",),
        ),
    )

    assert labels_route.called
    assert json.loads(create_route.calls[0].request.content) == {
        "title": "Tune workload",
        "head": "greenkube/plan-12",
        "base": "main",
        "body": "Rich plan body",
        "labels": [13, 17],
        "reviewers": ["octocat"],
        "team_reviewers": ["platform"],
    }
    assert pull_request.number == 12
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_rejects_draft_option_before_any_provider_request():
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    with pytest.raises(GitProviderCapabilityError, match="draft_pull_request"):
        await provider.create_pull_request(
            repo,
            head="greenkube/plan-13",
            base="main",
            title="Tune workload",
            body="Rich plan body",
            options=PullRequestOptions(draft=True, labels=("greenkube",)),
        )

    assert len(respx.calls) == 0
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_contents_fallback_returns_every_file_for_safe_tree_verification():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    respx.get(f"{api}/git/trees/main").mock(return_value=Response(404, json={"message": "Not Found"}))
    respx.get(f"{api}/contents").mock(
        return_value=Response(
            200,
            json=[
                {"type": "file", "path": "README.md"},
                {"type": "dir", "path": "apps"},
            ],
        )
    )
    respx.get(f"{api}/contents/apps").mock(
        return_value=Response(
            200,
            json=[
                {"type": "file", "path": "apps/deploy.yaml"},
                {"type": "file", "path": "apps/image.tar"},
            ],
        )
    )
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    assert await provider.list_files(repo, "main") == ["README.md", "apps/deploy.yaml", "apps/image.tar"]
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_contents_fallback_fails_closed_on_unsupported_entries():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    respx.get(f"{api}/git/trees/main").mock(return_value=Response(404, json={"message": "Not Found"}))
    respx.get(f"{api}/contents").mock(
        return_value=Response(200, json=[{"type": "symlink", "path": "current", "target": "deploy.yaml"}])
    )
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    with pytest.raises(GitProviderError, match="unsupported"):
        await provider.list_files(repo, "main")
    await provider.close()


class TestValidateGitRef:
    @pytest.mark.parametrize(
        "ref",
        ["../main", "feature..x", "branch name", "a@{1}", "-bad", "/leading", "trailing/", "x.lock", ""],
    )
    def test_rejects_invalid_refs(self, ref):
        with pytest.raises(GitProviderError):
            validate_git_ref(ref)

    @pytest.mark.parametrize("ref", ["main", "feature/greenkube-reco-1", "release-1.2"])
    def test_accepts_valid_refs(self, ref):
        assert validate_git_ref(ref) == ref


@pytest.mark.asyncio
@respx.mock
async def test_github_get_file_percent_encodes_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return Response(200, json={"content": base64.b64encode(b"x").decode(), "sha": "s"})

    respx.get(url__startswith="https://api.github.com/repos/acme/manifests/contents/").mock(side_effect=handler)
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    file = await provider.get_file(repo, "apps space/api.yaml", "main")

    assert file is not None and file.sha == "s"
    assert "%20" in seen["url"]


@pytest.mark.asyncio
@respx.mock
async def test_github_create_pull_request_404_raises():
    respx.post("https://api.github.com/repos/acme/manifests/pulls").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    with pytest.raises(GitProviderError):
        await provider.create_pull_request(repo, head="feature/x", base="main", title="t", body="b")


def _pull_request_lookup_case(provider_name):
    if provider_name == "github":
        provider = GitHubProvider("token")
        repo = GitRepository(provider="github", owner="acme", name="manifests")
        endpoint = "https://api.github.com/repos/acme/manifests/pulls"
        page_size = 100
        open_state = "open"
        query = {
            "state": "open",
            "head": "acme:feature/green-kube",
            "base": "release/stable",
            "per_page": "100",
        }
    elif provider_name == "gitlab":
        provider = GitLabProvider("token")
        repo = GitRepository(provider="gitlab", owner="acme", name="manifests")
        endpoint = "https://gitlab.com/api/v4/projects/acme%2Fmanifests/merge_requests"
        page_size = 100
        open_state = "opened"
        query = {
            "state": "opened",
            "source_branch": "feature/green-kube",
            "target_branch": "release/stable",
            "per_page": "100",
        }
    else:
        provider = GiteaProvider("token", api_base_url="http://gitea.local")
        repo = GitRepository(provider="gitea", owner="acme", name="manifests")
        endpoint = "http://gitea.local/api/v1/repos/acme/manifests/pulls"
        page_size = 50
        open_state = "open"
        query = {"state": "open", "limit": "50"}
    return provider, repo, endpoint, page_size, open_state, query


def _pull_request_lookup_payload(provider_name, *, number, head, base, state):
    url = {
        "github": f"https://github.com/acme/manifests/pull/{number}",
        "gitlab": f"https://gitlab.com/acme/manifests/-/merge_requests/{number}",
        "gitea": f"http://gitea.local/acme/manifests/pulls/{number}",
    }[provider_name]
    payload = {
        "number" if provider_name != "gitlab" else "iid": number,
        "html_url" if provider_name != "gitlab" else "web_url": url,
        "state": state,
    }
    if provider_name == "gitlab":
        payload.update(source_branch=head, target_branch=base)
    else:
        payload.update(head={"ref": head}, base={"ref": base})
    return payload


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_returns_only_an_exact_open_request(provider_name):
    provider, repo, endpoint, _, open_state, expected_query = _pull_request_lookup_case(provider_name)
    head = "feature/green-kube"
    base = "release/stable"
    records = [
        _pull_request_lookup_payload(provider_name, number=1, head="feature/other", base=base, state=open_state),
        _pull_request_lookup_payload(provider_name, number=2, head=head, base="main", state=open_state),
        _pull_request_lookup_payload(provider_name, number=3, head=head, base=base, state="closed"),
        _pull_request_lookup_payload(provider_name, number=4, head=head, base=base, state=open_state),
    ]
    requests = []
    route = respx.get(endpoint).mock(
        side_effect=lambda request: (requests.append(request), Response(200, json=records))[1]
    )

    pull_request = await provider.find_pull_request(repo, head=head, base=base)

    assert pull_request is not None
    assert isinstance(pull_request, PullRequestInfo)
    assert pull_request.number == 4
    assert pull_request.status == open_state
    assert route.call_count == 1
    assert requests[0].url.params["state"] == expected_query["state"]
    assert requests[0].url.params["page"] == "1"
    for key, value in expected_query.items():
        assert requests[0].url.params[key] == value
    if provider_name != "gitea":
        assert "%2F" in requests[0].url.query.decode().upper()
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_returns_none_when_no_open_requests_exist(provider_name):
    provider, repo, endpoint, _, _, _ = _pull_request_lookup_case(provider_name)
    respx.get(endpoint).mock(return_value=Response(200, json=[]))

    pull_request = await provider.find_pull_request(repo, head="feature/green-kube", base="release/stable")

    assert pull_request is None
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_ignores_wrong_head_base_or_closed_request(provider_name):
    provider, repo, endpoint, _, open_state, _ = _pull_request_lookup_case(provider_name)
    head = "feature/green-kube"
    base = "release/stable"
    respx.get(endpoint).mock(
        return_value=Response(
            200,
            json=[
                _pull_request_lookup_payload(
                    provider_name, number=1, head="feature/other", base=base, state=open_state
                ),
                _pull_request_lookup_payload(provider_name, number=2, head=head, base="main", state=open_state),
                _pull_request_lookup_payload(provider_name, number=3, head=head, base=base, state="closed"),
            ],
        )
    )

    pull_request = await provider.find_pull_request(repo, head=head, base=base)

    assert pull_request is None
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_follows_pagination(provider_name):
    provider, repo, endpoint, page_size, open_state, _ = _pull_request_lookup_case(provider_name)
    requests = []
    first_page = [
        _pull_request_lookup_payload(
            provider_name,
            number=index + 1,
            head=f"feature/unrelated-{index}",
            base="main",
            state=open_state,
        )
        for index in range(page_size if provider_name != "gitea" else 1)
    ]
    match = _pull_request_lookup_payload(
        provider_name,
        number=page_size + 1,
        head="feature/green-kube",
        base="release/stable",
        state=open_state,
    )

    def handler(request):
        requests.append(request)
        page = request.url.params["page"]
        if page == "1":
            headers = {}
            if provider_name == "gitlab":
                headers["x-next-page"] = "2"
            elif provider_name == "gitea":
                headers["link"] = (
                    '<http://gitea.local/api/v1/repos/acme/manifests/pulls?state=open&limit=50&page=2>; rel="next"'
                )
            return Response(200, json=first_page, headers=headers)
        return Response(200, json=[match], headers={"x-next-page": ""} if provider_name == "gitlab" else {})

    respx.get(endpoint).mock(side_effect=handler)

    pull_request = await provider.find_pull_request(repo, head="feature/green-kube", base="release/stable")

    assert pull_request is not None
    assert pull_request.number == page_size + 1
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_wraps_provider_errors(provider_name):
    provider, repo, endpoint, _, _, _ = _pull_request_lookup_case(provider_name)
    respx.get(endpoint).mock(return_value=Response(401, json={"message": "denied"}))

    with pytest.raises(GitProviderError, match="denied"):
        await provider.find_pull_request(repo, head="feature/green-kube", base="release/stable")

    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_rejects_malformed_provider_lists(provider_name):
    provider, repo, endpoint, _, _, _ = _pull_request_lookup_case(provider_name)
    respx.get(endpoint).mock(return_value=Response(200, json={"pull_requests": []}))

    with pytest.raises(GitProviderError, match="invalid pull-request list"):
        await provider.find_pull_request(repo, head="feature/green-kube", base="release/stable")

    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_pull_request_rejects_malformed_request_items(provider_name):
    provider, repo, endpoint, _, open_state, _ = _pull_request_lookup_case(provider_name)
    payload = _pull_request_lookup_payload(
        provider_name,
        number=7,
        head="feature/green-kube",
        base="release/stable",
        state=open_state,
    )
    payload.pop("iid" if provider_name == "gitlab" else "number")
    respx.get(endpoint).mock(return_value=Response(200, json=[payload]))

    with pytest.raises(GitProviderError, match="invalid pull-request number"):
        await provider.find_pull_request(repo, head="feature/green-kube", base="release/stable")

    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_find_open_pull_request_for_branch_checks_head_and_base(provider_name):
    provider, repo, endpoint, page_size, open_state, _ = _pull_request_lookup_case(provider_name)
    branch = "greenkube/plan/" + "a" * 32
    first_page = [
        _pull_request_lookup_payload(
            provider_name,
            number=index + 1,
            head=f"feature/unrelated-{index}",
            base="main",
            state=open_state,
        )
        for index in range(page_size)
    ]
    match = _pull_request_lookup_payload(
        provider_name,
        number=page_size + 1,
        head="feature/other",
        base=branch,
        state=open_state,
    )
    requests = []

    def handler(request):
        requests.append(request)
        if provider_name == "github":
            if "base" in request.url.params:
                return Response(200, json=[match])
            return Response(200, json=first_page if request.url.params["page"] == "1" else [])
        if provider_name == "gitlab":
            if "target_branch" in request.url.params:
                return Response(200, json=[match], headers={"x-next-page": ""})
            return Response(200, json=[], headers={"x-next-page": ""})
        if request.url.params["page"] == "1":
            headers = {}
            headers["link"] = (
                "<http://gitea.local/api/v1/repos/acme/manifests/pulls?state=open&limit=50&page=2>; rel=next"
            )
            return Response(200, json=first_page, headers=headers)
        return Response(200, json=[match])

    respx.get(endpoint).mock(side_effect=handler)

    pull_request = await provider.find_open_pull_request_for_branch(repo, branch)

    assert pull_request is not None
    assert pull_request.number == page_size + 1
    if provider_name == "github":
        assert requests[0].url.params["head"] == f"{repo.owner}:{branch}"
        assert requests[1].url.params["head"] == f"{repo.owner}:{branch}"
        assert requests[2].url.params["base"] == branch
    elif provider_name == "gitlab":
        assert requests[0].url.params["source_branch"] == branch
        assert requests[1].url.params["target_branch"] == branch
    else:
        assert [request.url.params["page"] for request in requests] == ["1", "2"]
    await provider.close()


@pytest.mark.parametrize(
    ("provider_name", "delete_endpoint"),
    [
        ("github", "https://api.github.com/repos/acme/manifests/git/refs/heads/greenkube/plan/"),
        ("gitlab", "https://gitlab.com/api/v4/projects/acme%2Fmanifests/repository/branches/greenkube%2Fplan%2F"),
        ("gitea", "http://gitea.local/api/v1/repos/acme/manifests/branches/greenkube/plan/"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_delete_plan_branch_is_scoped_and_missing_refs_are_idempotent(provider_name, delete_endpoint):
    provider, repo, _, _, _, _ = _pull_request_lookup_case(provider_name)
    branch = "greenkube/plan/" + "c" * 32
    provider.get_default_branch = AsyncMock(return_value="main")
    provider.find_open_pull_request_for_branch = AsyncMock(return_value=None)
    endpoint = delete_endpoint + "c" * 32
    route = respx.delete(endpoint).mock(return_value=Response(404, json={"message": "not found"}))

    await provider.delete_plan_branch(repo, branch)

    assert route.call_count == 1
    provider.find_open_pull_request_for_branch.assert_awaited_once_with(repo, branch)
    with pytest.raises(GitProviderError, match="deterministic GreenKube plan namespace"):
        await provider.delete_plan_branch(repo, "feature/unrelated")
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_delete_plan_branch_refuses_default_and_open_pull_request(provider_name):
    provider, repo, _, _, _, _ = _pull_request_lookup_case(provider_name)
    branch = "greenkube/plan/" + "d" * 32
    repo.default_branch = branch
    provider.get_default_branch = AsyncMock(return_value="main")
    provider.find_open_pull_request_for_branch = AsyncMock(return_value=None)
    with pytest.raises(GitProviderError, match="plan base branch"):
        await provider.delete_plan_branch(repo, branch)

    repo.default_branch = "main"
    provider.get_default_branch = AsyncMock(return_value=branch)
    with pytest.raises(GitProviderError, match="default branch"):
        await provider.delete_plan_branch(repo, branch)

    provider.get_default_branch = AsyncMock(return_value="main")
    provider.find_open_pull_request_for_branch = AsyncMock(
        return_value=PullRequestInfo(
            number=9,
            url="https://example.invalid/pr/9",
            status="open",
            head_branch=branch,
            base_branch="release",
        )
    )
    with pytest.raises(GitProviderError, match="open pull request"):
        await provider.delete_plan_branch(repo, branch)
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_list_files_follows_pagination():
    respx.get("https://gitlab.com/api/v4/projects/acme%2Fmanifests/repository/tree").mock(
        side_effect=[
            Response(200, json=[{"path": "a.yaml", "type": "blob"}], headers={"x-next-page": "2"}),
            Response(200, json=[{"path": "b.yaml", "type": "blob"}], headers={"x-next-page": ""}),
        ]
    )
    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/acme/manifests.git")

    files = await provider.list_files(repo, "main")

    assert files == ["a.yaml", "b.yaml"]


def _commit_request(repo, files):
    return GitCommitRequest(
        repo=repo,
        branch="feature/atomic",
        base_revision="base-sha",
        message="apply plan",
        files=files,
        author_name="GreenKube Bot",
        author_email="bot@greenkube.cloud",
    )


def test_git_commit_contract_validates_file_actions_and_paths():
    with pytest.raises(GitProviderError):
        GitFileEdit(path="../secret.yaml", action="create", content="x")
    with pytest.raises(GitProviderError):
        GitFileEdit(path="apps/api.yaml", action="create")
    with pytest.raises(GitProviderError):
        GitFileEdit(path="apps/api.yaml", action="delete", content="x")

    repo = GitRepository(provider="github", owner="acme", name="manifests")
    edit = GitFileEdit(path="apps/api.yaml", action="update", content="x")
    with pytest.raises(GitProviderError, match="duplicate"):
        _commit_request(repo, [edit, edit])


@pytest.mark.asyncio
@respx.mock
async def test_github_create_commit_uses_one_git_data_commit_for_file_edits():
    api = "https://api.github.com/repos/acme/manifests"
    seen = {}
    respx.get(f"{api}/git/ref/heads/feature/atomic").mock(
        return_value=Response(200, json={"object": {"sha": "base-sha"}})
    )
    respx.get(f"{api}/git/commits/base-sha").mock(return_value=Response(200, json={"tree": {"sha": "base-tree"}}))
    respx.get(f"{api}/contents/new.yaml").mock(return_value=Response(404, json={"message": "Not Found"}))
    respx.get(f"{api}/contents/apps/update.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "sha": "update-blob"},
        )
    )
    respx.get(f"{api}/contents/apps/delete.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "sha": "delete-blob"},
        )
    )

    def create_tree(request):
        seen["tree"] = json.loads(request.content)
        return Response(201, json={"sha": "new-tree"})

    def create_commit(request):
        seen["commit"] = json.loads(request.content)
        return Response(201, json={"sha": "new-commit"})

    def update_ref(request):
        seen["ref"] = json.loads(request.content)
        return Response(200, json={"object": {"sha": "new-commit"}})

    tree_route = respx.post(f"{api}/git/trees").mock(side_effect=create_tree)
    commit_route = respx.post(f"{api}/git/commits").mock(side_effect=create_commit)
    ref_route = respx.patch(f"{api}/git/refs/heads/feature/atomic").mock(side_effect=update_ref)

    provider = GitHubProvider("token")
    repo = GitRepository(provider="github", owner="acme", name="manifests")
    request = _commit_request(
        repo,
        [
            GitFileEdit(path="new.yaml", action="create", content="new"),
            GitFileEdit(path="apps/update.yaml", action="update", content="updated"),
            GitFileEdit(path="apps/delete.yaml", action="delete"),
        ],
    )

    result = await provider.create_commit(request)

    assert isinstance(result, GitCommitResult)
    assert result.commit_sha == "new-commit"
    assert result.branch == "feature/atomic"
    assert result.files_changed == ["new.yaml", "apps/update.yaml", "apps/delete.yaml"]
    assert tree_route.call_count == 1
    assert commit_route.call_count == 1
    assert ref_route.call_count == 1
    assert seen["tree"]["base_tree"] == "base-tree"
    assert seen["tree"]["tree"] == [
        {"path": "new.yaml", "mode": "100644", "type": "blob", "content": "new"},
        {"path": "apps/update.yaml", "mode": "100644", "type": "blob", "content": "updated"},
        {"path": "apps/delete.yaml", "mode": "100644", "type": "blob", "sha": None},
    ]
    assert seen["commit"]["parents"] == ["base-sha"]
    assert seen["commit"]["tree"] == "new-tree"
    assert seen["commit"]["message"] == "apply plan"
    assert seen["commit"]["author"] == {"name": "GreenKube Bot", "email": "bot@greenkube.cloud"}
    assert seen["commit"]["committer"] == {"name": "GreenKube Bot", "email": "bot@greenkube.cloud"}
    assert seen["ref"] == {"sha": "new-commit", "force": False}
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_create_commit_rejects_a_stale_base_before_writing_objects():
    api = "https://api.github.com/repos/acme/manifests"
    respx.get(f"{api}/git/ref/heads/feature/atomic").mock(
        return_value=Response(200, json={"object": {"sha": "newer-sha"}})
    )
    tree_route = respx.post(f"{api}/git/trees").mock(return_value=Response(201, json={"sha": "unused"}))

    provider = GitHubProvider("token")
    repo = GitRepository(provider="github", owner="acme", name="manifests")

    with pytest.raises(GitConflictError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )

    assert tree_route.call_count == 0
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_create_commit_maps_non_fast_forward_ref_update_to_conflict():
    api = "https://api.github.com/repos/acme/manifests"
    respx.get(f"{api}/git/ref/heads/feature/atomic").mock(
        return_value=Response(200, json={"object": {"sha": "base-sha"}})
    )
    respx.get(f"{api}/git/commits/base-sha").mock(return_value=Response(200, json={"tree": {"sha": "base-tree"}}))
    respx.get(f"{api}/contents/apps/api.yaml").mock(
        return_value=Response(200, json={"content": base64.b64encode(b"old").decode(), "sha": "old-blob"})
    )
    respx.post(f"{api}/git/trees").mock(return_value=Response(201, json={"sha": "new-tree"}))
    respx.post(f"{api}/git/commits").mock(return_value=Response(201, json={"sha": "new-commit"}))
    respx.patch(f"{api}/git/refs/heads/feature/atomic").mock(
        return_value=Response(422, json={"message": "Update is not a fast forward"})
    )

    provider = GitHubProvider("token")
    repo = GitRepository(provider="github", owner="acme", name="manifests")

    with pytest.raises(GitConflictError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_create_commit_uses_native_actions_and_expected_parent():
    api = "https://gitlab.com/api/v4/projects/group%2Fsubgroup%2Fmanifests"
    seen = {}
    respx.get(f"{api}/repository/branches/feature%2Fatomic").mock(
        return_value=Response(200, json={"commit": {"id": "base-sha"}})
    )
    respx.get(f"{api}/repository/files/new.yaml").mock(return_value=Response(404, json={"message": "File not found"}))
    respx.get(f"{api}/repository/files/apps%2Fupdate.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "last_commit_id": "update-last-commit"},
        )
    )
    respx.get(f"{api}/repository/files/apps%2Fdelete.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "last_commit_id": "delete-last-commit"},
        )
    )

    def create_commit(request):
        seen["commit"] = json.loads(request.content)
        return Response(201, json={"id": "new-commit", "parent_ids": ["base-sha"]})

    commit_route = respx.post(f"{api}/repository/commits").mock(side_effect=create_commit)
    provider = GitLabProvider("token")
    repo = GitRepository(provider="gitlab", owner="group/subgroup", name="manifests")

    result = await provider.create_commit(
        _commit_request(
            repo,
            [
                GitFileEdit(path="new.yaml", action="create", content="new"),
                GitFileEdit(path="apps/update.yaml", action="update", content="updated"),
                GitFileEdit(path="apps/delete.yaml", action="delete"),
            ],
        )
    )

    assert isinstance(result, GitCommitResult)
    assert result.commit_sha == "new-commit"
    assert result.files_changed == ["new.yaml", "apps/update.yaml", "apps/delete.yaml"]
    assert commit_route.call_count == 1
    assert seen["commit"]["branch"] == "feature/atomic"
    assert seen["commit"]["start_sha"] == "base-sha"
    assert seen["commit"]["force"] is False
    assert seen["commit"]["commit_message"] == "apply plan"
    assert seen["commit"]["author_name"] == "GreenKube Bot"
    assert seen["commit"]["author_email"] == "bot@greenkube.cloud"
    assert seen["commit"]["actions"] == [
        {"action": "create", "file_path": "new.yaml", "content": "new", "encoding": "text"},
        {
            "action": "update",
            "file_path": "apps/update.yaml",
            "content": "updated",
            "encoding": "text",
            "last_commit_id": "update-last-commit",
        },
        {
            "action": "delete",
            "file_path": "apps/delete.yaml",
            "last_commit_id": "delete-last-commit",
        },
    ]
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_create_commit_rejects_a_stale_base_before_committing():
    api = "https://gitlab.com/api/v4/projects/acme%2Fmanifests"
    respx.get(f"{api}/repository/branches/feature%2Fatomic").mock(
        return_value=Response(200, json={"commit": {"id": "newer-sha"}})
    )
    commit_route = respx.post(f"{api}/repository/commits").mock(return_value=Response(201, json={"id": "unused"}))

    provider = GitLabProvider("token")
    repo = GitRepository(provider="gitlab", owner="acme", name="manifests")

    with pytest.raises(GitConflictError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )

    assert commit_route.call_count == 0
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_atomic_commit_post_is_not_retried_after_transport_failure():
    api = "https://gitlab.com/api/v4/projects/acme%2Fmanifests"
    respx.get(f"{api}/repository/branches/feature%2Fatomic").mock(
        return_value=Response(200, json={"commit": {"id": "base-sha"}})
    )
    respx.get(f"{api}/repository/files/apps%2Fapi.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "last_commit_id": "old-last-commit"},
        )
    )
    commit_route = respx.post(f"{api}/repository/commits").mock(
        side_effect=[
            ConnectError("connection lost"),
            Response(201, json={"id": "new-commit", "parent_ids": ["base-sha"]}),
        ]
    )

    provider = GitLabProvider("token")
    repo = GitRepository(provider="gitlab", owner="acme", name="manifests")
    with pytest.raises(GitProviderTransientError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )

    assert commit_route.call_count == 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_create_commit_uses_one_native_atomic_multi_file_commit():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    seen = {}
    respx.get(f"{api}/branches/feature/atomic").mock(return_value=Response(200, json={"commit": {"id": "base-sha"}}))

    def create_staging_branch(request):
        seen["staging_create"] = json.loads(request.content)
        seen["staging_branch"] = seen["staging_create"]["new_branch_name"]
        return Response(201, json={"name": seen["staging_branch"]})

    def get_staging_branch(request):
        return Response(200, json={"commit": {"id": "base-sha"}})

    staging_create_route = respx.post(f"{api}/branches").mock(side_effect=create_staging_branch)
    staging_get_route = respx.get(url__startswith=f"{api}/branches/greenkube-commit-").mock(
        side_effect=get_staging_branch
    )
    respx.get(f"{api}/contents/new.yaml").mock(return_value=Response(404, json={"message": "Not Found"}))
    respx.get(f"{api}/contents/apps%2Fupdate.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "sha": "update-blob"},
        )
    )
    respx.get(f"{api}/contents/apps%2Fdelete.yaml").mock(
        return_value=Response(
            200,
            json={"content": base64.b64encode(b"old").decode(), "sha": "delete-blob"},
        )
    )

    def create_commit(request):
        seen["commit"] = json.loads(request.content)
        return Response(
            201,
            json={"commit": {"sha": "new-commit", "parents": [{"sha": "base-sha"}]}},
        )

    def update_branch(request):
        seen["branch_update"] = json.loads(request.content)
        return Response(204)

    def delete_staging_branch(request):
        seen["deleted_staging_branch"] = str(request.url).rsplit("/", 1)[-1]
        return Response(204)

    commit_route = respx.post(f"{api}/contents").mock(side_effect=create_commit)
    single_file_route = respx.post(url__startswith=f"{api}/contents/").mock(
        return_value=Response(201, json={"commit": {"sha": "unused"}})
    )
    branch_update_route = respx.put(f"{api}/branches/feature/atomic").mock(side_effect=update_branch)
    staging_delete_route = respx.delete(url__startswith=f"{api}/branches/greenkube-commit-").mock(
        side_effect=delete_staging_branch
    )

    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = GitRepository(provider="gitea", owner="acme", name="manifests")
    result = await provider.create_commit(
        _commit_request(
            repo,
            [
                GitFileEdit(path="new.yaml", action="create", content="new"),
                GitFileEdit(path="apps/update.yaml", action="update", content="updated"),
                GitFileEdit(path="apps/delete.yaml", action="delete"),
            ],
        )
    )

    assert provider.supports("multi_file_commit")
    assert isinstance(result, GitCommitResult)
    assert result.commit_sha == "new-commit"
    assert result.branch == "feature/atomic"
    assert result.files_changed == ["new.yaml", "apps/update.yaml", "apps/delete.yaml"]
    assert staging_create_route.call_count == 1
    assert staging_get_route.call_count == 1
    assert commit_route.call_count == 1
    assert single_file_route.call_count == 0
    assert branch_update_route.call_count == 1
    assert staging_delete_route.call_count == 1
    assert seen["staging_create"]["old_branch_name"] == "feature/atomic"
    assert seen["staging_create"]["new_branch_name"].startswith("greenkube-commit-")
    assert seen["commit"] == {
        "branch": seen["staging_branch"],
        "message": "apply plan",
        "author": {"name": "GreenKube Bot", "email": "bot@greenkube.cloud"},
        "committer": {"name": "GreenKube Bot", "email": "bot@greenkube.cloud"},
        "files": [
            {
                "operation": "create",
                "path": "new.yaml",
                "content": base64.b64encode(b"new").decode("ascii"),
            },
            {
                "operation": "update",
                "path": "apps/update.yaml",
                "content": base64.b64encode(b"updated").decode("ascii"),
                "sha": "update-blob",
            },
            {"operation": "delete", "path": "apps/delete.yaml", "sha": "delete-blob"},
        ],
    }
    assert seen["branch_update"] == {
        "new_commit_id": "new-commit",
        "old_commit_id": "base-sha",
        "force": False,
    }
    assert seen["deleted_staging_branch"] == seen["staging_branch"]
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_create_commit_rejects_a_stale_base_without_staging():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    respx.get(f"{api}/branches/feature/atomic").mock(return_value=Response(200, json={"commit": {"id": "newer-sha"}}))
    staging_create_route = respx.post(f"{api}/branches").mock(return_value=Response(201, json={"name": "unused"}))

    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = GitRepository(provider="gitea", owner="acme", name="manifests")
    with pytest.raises(GitConflictError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )

    assert staging_create_route.call_count == 0
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_create_commit_maps_compare_and_swap_failure_to_conflict():
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    respx.get(f"{api}/branches/feature/atomic").mock(return_value=Response(200, json={"commit": {"id": "base-sha"}}))

    def create_staging_branch(request):
        body = json.loads(request.content)
        return Response(201, json={"name": body["new_branch_name"]})

    respx.post(f"{api}/branches").mock(side_effect=create_staging_branch)
    respx.get(url__startswith=f"{api}/branches/greenkube-commit-").mock(
        return_value=Response(200, json={"commit": {"id": "base-sha"}})
    )
    respx.get(f"{api}/contents/apps%2Fapi.yaml").mock(
        return_value=Response(200, json={"content": base64.b64encode(b"old").decode(), "sha": "old-blob"})
    )
    respx.post(f"{api}/contents").mock(
        return_value=Response(201, json={"commit": {"sha": "new-commit", "parents": [{"sha": "base-sha"}]}})
    )
    respx.put(f"{api}/branches/feature/atomic").mock(return_value=Response(409, json={"message": "branch tip changed"}))
    staging_delete_route = respx.delete(url__startswith=f"{api}/branches/greenkube-commit-").mock(
        return_value=Response(204)
    )

    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = GitRepository(provider="gitea", owner="acme", name="manifests")
    with pytest.raises(GitConflictError):
        await provider.create_commit(
            _commit_request(repo, [GitFileEdit(path="apps/api.yaml", action="update", content="updated")])
        )

    assert staging_delete_route.call_count == 1
    await provider.close()


def _ref_revision_provider_case(provider_name):
    repo = GitRepository(provider=provider_name, owner="acme", name="manifests")
    if provider_name == "github":
        api = "https://api.github.com/repos/acme/manifests"
        return (
            GitHubProvider("token"),
            repo,
            {
                "branch": f"{api}/git/ref/heads/release-1",
                "tag": f"{api}/git/ref/tags/release-1",
                "tag_object": f"{api}/git/tags/{'b' * 40}",
            },
        )
    if provider_name == "gitlab":
        api = "https://gitlab.com/api/v4/projects/acme%2Fmanifests/repository"
        return (
            GitLabProvider("token"),
            repo,
            {
                "branch": f"{api}/branches/release-1",
                "tag": f"{api}/tags/release-1",
            },
        )
    api = "http://gitea.local/api/v1/repos/acme/manifests"
    return (
        GiteaProvider("token", api_base_url="http://gitea.local"),
        repo,
        {
            "branch": f"{api}/branches/release-1",
            "tag": f"{api}/git/refs/tags/release-1",
            "tag_object": f"{api}/git/tags/{'b' * 40}",
        },
    )


def _ref_revision_payload(provider_name, sha, *, tag=False):
    if provider_name == "github":
        return {"object": {"sha": sha, "type": "commit"}}
    if provider_name == "gitlab":
        return {"commit": {"id": sha}}
    if not tag:
        return {"commit": {"id": sha}}
    return [{"ref": "refs/tags/release-1", "object": {"sha": sha, "type": "commit"}}]


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_resolves_branch(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    sha = "a" * 40
    route = respx.get(endpoints["branch"]).mock(
        return_value=Response(200, json=_ref_revision_payload(provider_name, sha))
    )

    assert await provider.get_ref_revision(repo, "release-1") == sha
    assert route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_resolves_tag(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    sha = "c" * 40
    respx.get(endpoints["branch"]).mock(return_value=Response(404, json={"message": "Not Found"}))
    tag_route = respx.get(endpoints["tag"]).mock(
        return_value=Response(200, json=_ref_revision_payload(provider_name, sha, tag=True))
    )

    assert await provider.get_ref_revision(repo, "release-1") == sha
    assert tag_route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_rejects_malformed_response(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    route = respx.get(endpoints["branch"]).mock(return_value=Response(200, json={}))

    with pytest.raises(GitProviderError, match="invalid"):
        await provider.get_ref_revision(repo, "release-1")

    assert route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_rejects_invalid_json(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    route = respx.get(endpoints["branch"]).mock(return_value=Response(200, text="not-json"))

    with pytest.raises(GitProviderError, match="invalid JSON"):
        await provider.get_ref_revision(repo, "release-1")

    assert route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_rejects_non_full_commit_sha(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    route = respx.get(endpoints["branch"]).mock(
        return_value=Response(200, json=_ref_revision_payload(provider_name, "abc123"))
    )

    with pytest.raises(GitProviderError, match="invalid full commit SHA"):
        await provider.get_ref_revision(repo, "release-1")

    assert route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_reports_missing_ref(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    branch_route = respx.get(endpoints["branch"]).mock(return_value=Response(404, json={"message": "Not Found"}))
    tag_route = respx.get(endpoints["tag"]).mock(return_value=Response(404, json={"message": "Not Found"}))

    with pytest.raises(GitProviderError, match="not found"):
        await provider.get_ref_revision(repo, "release-1")

    assert branch_route.call_count == 1
    assert tag_route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_dereferences_annotated_tags(provider_name):
    provider, repo, endpoints = _ref_revision_provider_case(provider_name)
    object_sha = "b" * 40
    commit_sha = "d" * 40
    respx.get(endpoints["branch"]).mock(return_value=Response(404, json={"message": "Not Found"}))
    if provider_name == "gitea":
        tag_payload = [{"ref": "refs/tags/release-1", "object": {"sha": object_sha, "type": "tag"}}]
    else:
        tag_payload = {"object": {"sha": object_sha, "type": "tag"}}
    respx.get(endpoints["tag"]).mock(return_value=Response(200, json=tag_payload))
    tag_object_route = respx.get(endpoints["tag_object"]).mock(
        return_value=Response(200, json={"object": {"sha": commit_sha, "type": "commit"}})
    )

    assert await provider.get_ref_revision(repo, "release-1") == commit_sha
    assert tag_object_route.call_count == 1
    await provider.close()


@pytest.mark.parametrize("provider_name", ["github", "gitlab", "gitea"])
@pytest.mark.asyncio
@respx.mock
async def test_get_ref_revision_rejects_invalid_ref_before_request(provider_name):
    provider, repo, _ = _ref_revision_provider_case(provider_name)

    with pytest.raises(GitProviderError, match="Invalid"):
        await provider.get_ref_revision(repo, "../release-1")

    assert not respx.calls
    await provider.close()
