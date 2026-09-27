# tests/automation/test_git_providers.py
"""Tests for the GitHub, GitLab and Gitea provider adapters (respx-mocked)."""

import base64

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import GitProviderError, parse_repo_url
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider


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
    assert git_file is not None and git_file.sha == "abc123" and git_file.content == content
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", content, "msg", "greenkube/reco-1", sha="abc123")
    pr = await provider.create_pull_request(repo, head="greenkube/reco-1", base="main", title="t", body="b")
    assert pr["number"] == 7
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
    assert git_file is not None
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", "content", "msg", "greenkube/reco-1")
    pr = await provider.create_pull_request(repo, head="h", base="main", title="t", body="b")
    assert pr["number"] == 3
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
    assert git_file is not None and git_file.sha == "abc"
    await provider.create_branch(repo, "greenkube/reco-1", "main")
    await provider.update_file(repo, "apps/api.yaml", content, "msg", "greenkube/reco-1", sha="abc")
    pr = await provider.create_pull_request(repo, head="h", base="main", title="t", body="b")
    assert pr["number"] == 11
    await provider.close()
