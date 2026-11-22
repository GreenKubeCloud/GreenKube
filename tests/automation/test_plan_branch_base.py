"""Plan branches start at the exact commit the plan was validated on, never at the moving ref."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import GitProviderError
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider

BASE_SHA = "a" * 40
GITHUB_API = "https://api.github.com/repos/acme/manifests"
GITEA_API = "http://gitea.local/api/v1/repos/acme/manifests"


@pytest.mark.asyncio
@respx.mock
async def test_github_creates_a_plan_branch_at_the_exact_commit_without_reading_the_moving_ref():
    moving_ref = respx.get(f"{GITHUB_API}/git/ref/heads/main").mock(
        return_value=Response(200, json={"object": {"sha": "b" * 40}})
    )
    created = respx.post(f"{GITHUB_API}/git/refs").mock(return_value=Response(201, json={}))
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    await provider.create_branch(repo, "greenkube/plan/abc", BASE_SHA)

    assert moving_ref.call_count == 0
    assert json.loads(created.calls[0].request.content) == {"ref": "refs/heads/greenkube/plan/abc", "sha": BASE_SHA}
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_creates_a_plan_branch_at_the_exact_commit():
    created = respx.post("https://gitlab.com/api/v4/projects/acme%2Fmanifests/repository/branches").mock(
        return_value=Response(201, json={"name": "greenkube/plan/abc", "commit": {"id": BASE_SHA}})
    )
    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/acme/manifests.git")

    await provider.create_branch(repo, "greenkube/plan/abc", BASE_SHA)

    assert created.calls[0].request.url.params["ref"] == BASE_SHA
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_creates_a_plan_branch_with_old_ref_name():
    created = respx.post(f"{GITEA_API}/branches").mock(
        return_value=Response(201, json={"name": "greenkube/plan/abc", "commit": {"id": BASE_SHA}})
    )
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    await provider.create_branch(repo, "greenkube/plan/abc", BASE_SHA)

    assert json.loads(created.calls[0].request.content) == {
        "new_branch_name": "greenkube/plan/abc",
        "old_ref_name": BASE_SHA,
    }
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_without_old_ref_name_fails_closed_and_removes_the_branch():
    # An older Gitea ignores old_ref_name and branches from the default branch head.
    respx.post(f"{GITEA_API}/branches").mock(
        return_value=Response(201, json={"name": "greenkube/plan/abc", "commit": {"id": "b" * 40}})
    )
    deleted = respx.delete(f"{GITEA_API}/branches/greenkube/plan/abc").mock(return_value=Response(204))
    provider = GiteaProvider("token", api_base_url="http://gitea.local")
    repo = provider.repository("http://gitea.local/acme/manifests.git")

    with pytest.raises(GitProviderError, match="cannot create a branch from a commit"):
        await provider.create_branch(repo, "greenkube/plan/abc", BASE_SHA)

    assert deleted.call_count == 1
    await provider.close()
