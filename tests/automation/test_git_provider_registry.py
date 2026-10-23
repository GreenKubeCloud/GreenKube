"""Tests for Git provider authorization and Secret-backed credentials."""

import json
from dataclasses import replace
from typing import cast

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import GitProviderCapability, GitProviderError, GitRepository, PullRequestOptions
from greenkube.automation.git.credentials import GitProviderCredentialError, SecretResolverError
from greenkube.automation.git.registry import (
    GitProviderRegistry,
    ProviderAuthorizationError,
    UnknownGitProviderError,
    provider_capabilities,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsProviderRepository

TOKEN = "secret-token-do-not-log"


class StubProviderRepository:
    def __init__(self, config: GitOpsProviderConfig, allowlists: list[str]):
        self.config = config
        self.allowlists = allowlists
        self.config_reads = 0
        self.allowlist_reads = 0

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        self.config_reads += 1
        return self.config if provider_id == self.config.id else None

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        self.allowlist_reads += 1
        return list(self.allowlists) if provider_id == self.config.id else []


class StubSecretResolver:
    def __init__(self, token: str | None = TOKEN, error: SecretResolverError | None = None):
        self.token = token
        self.error = error
        self.calls: list[tuple[str | None, str, str]] = []

    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str | None:
        self.calls.append((namespace, name, key))
        if self.error is not None:
            raise self.error
        return self.token


def _config(**overrides) -> GitOpsProviderConfig:
    values = {
        "id": "github-main",
        "provider_type": "github",
        "api_base_url": "https://api.github.test",
        "secret_namespace": "greenkube",
        "secret_name": "github-token",
        "secret_key": "token",
    }
    values.update(overrides)
    return GitOpsProviderConfig(**values)


def _repository() -> GitRepository:
    return GitRepository(provider="github", owner="acme", name="manifests")


def _repository_for_provider(provider: str) -> GitRepository:
    return GitRepository(provider=provider, owner="acme", name="manifests")


def _registry(
    config: GitOpsProviderConfig | None = None,
    *,
    allowlists: list[str] | None = None,
    secret_resolver: StubSecretResolver | None = None,
) -> tuple[GitProviderRegistry, StubProviderRepository, StubSecretResolver]:
    provider_repository = StubProviderRepository(config or _config(), allowlists or [])
    resolver = secret_resolver or StubSecretResolver()
    return (
        GitProviderRegistry(cast(GitOpsProviderRepository, provider_repository), resolver),
        provider_repository,
        resolver,
    )


@pytest.mark.asyncio
async def test_unknown_provider_type_is_rejected_before_allowlist_or_secret_lookup():
    registry, provider_repository, secret_resolver = _registry(
        replace(_config(), provider_type="not-a-supported-provider"),
        allowlists=["acme/*"],
    )

    with pytest.raises(UnknownGitProviderError):
        await registry.get_provider("github-main", _repository())

    assert provider_repository.allowlist_reads == 0
    assert secret_resolver.calls == []


@pytest.mark.asyncio
async def test_missing_provider_id_is_rejected_before_allowlist_or_secret_lookup():
    registry, provider_repository, secret_resolver = _registry(allowlists=["acme/*"])

    with pytest.raises(UnknownGitProviderError):
        await registry.get_provider("missing-provider", _repository())

    assert provider_repository.allowlist_reads == 0
    assert secret_resolver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_reference",
    [
        {},
        {"secret_namespace": "greenkube", "secret_name": "invalid/name", "secret_key": "token"},
        {"secret_name": "github-token", "secret_key": "invalid key"},
    ],
)
async def test_missing_or_invalid_secret_reference_is_rejected(secret_reference):
    values = {
        "secret_namespace": None,
        "secret_name": None,
        "secret_key": None,
    }
    values.update(secret_reference)
    registry, _, secret_resolver = _registry(
        _config(**values),
        allowlists=["acme/manifests"],
    )

    with pytest.raises(GitProviderCredentialError):
        await registry.get_provider("github-main", _repository())

    assert secret_resolver.calls == []


@pytest.mark.asyncio
async def test_secret_resolution_errors_are_redacted():
    registry, _, _ = _registry(
        allowlists=["acme/manifests"],
        secret_resolver=StubSecretResolver(error=SecretResolverError(TOKEN)),
    )

    with pytest.raises(GitProviderCredentialError) as error:
        await registry.get_provider("github-main", _repository())

    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_empty_secret_value_is_rejected():
    registry, _, secret_resolver = _registry(
        allowlists=["acme/manifests"],
        secret_resolver=StubSecretResolver(token="  "),
    )

    with pytest.raises(GitProviderCredentialError):
        await registry.get_provider("github-main", _repository())

    assert secret_resolver.calls == [("greenkube", "github-token", "token")]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlists", [[], ["other/*"]])
async def test_empty_or_non_matching_allowlist_denies_before_secret_or_git_calls(allowlists):
    registry, provider_repository, secret_resolver = _registry(allowlists=allowlists)

    with pytest.raises(ProviderAuthorizationError):
        await registry.get_provider("github-main", _repository())

    assert provider_repository.allowlist_reads == 1
    assert secret_resolver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["acme/manifests", "acme/*"])
async def test_exact_and_glob_allowlists_create_provider_without_git_api_call(pattern):
    registry, _, secret_resolver = _registry(allowlists=[pattern])

    with respx.mock:
        provider = await registry.get_provider("github-main", _repository())

    assert provider.name == "github"
    assert provider.capabilities == provider_capabilities("github")
    assert secret_resolver.calls == [("greenkube", "github-token", "token")]
    assert len(respx.calls) == 0
    assert provider_capabilities("github") == frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.FIND_PULL_REQUEST,
            GitProviderCapability.GET_REF_REVISION,
            GitProviderCapability.REPOSITORY_ARCHIVE,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.TEAM_REVIEWERS,
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            GitProviderCapability.DELETE_PLAN_BRANCH,
        }
    )


@pytest.mark.asyncio
async def test_authorized_gitea_provider_forwards_branch_cleanup_capabilities():
    provider_config = replace(
        _config(),
        provider_type="gitea",
        api_base_url="http://gitea.test/api/v1",
    )
    registry, provider_repository, _ = _registry(provider_config, allowlists=["acme/manifests"])
    repository = _repository_for_provider("gitea")
    branch = "greenkube/plan/0123456789abcdef0123456789abcdef"

    with respx.mock:
        open_pr_lookup = respx.get("http://gitea.test/api/v1/repos/acme/manifests/pulls").mock(
            return_value=Response(200, json=[])
        )
        respx.get("http://gitea.test/api/v1/repos/acme/manifests").mock(
            return_value=Response(200, json={"default_branch": "main"})
        )
        branch_delete = respx.delete(f"http://gitea.test/api/v1/repos/acme/manifests/branches/{branch}").mock(
            return_value=Response(204)
        )

        provider = await registry.get_provider(provider_config.id, repository)
        assert await provider.find_open_pull_request_for_branch(repository, branch) is None
        await provider.delete_plan_branch(repository, branch)

    assert open_pr_lookup.call_count == 2
    assert branch_delete.call_count == 1
    assert provider_repository.allowlist_reads == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "name", "pattern", "allowed"),
    [
        ("acme", "manifests", "acme/*", True),
        ("acme/team", "manifests", "acme/*", False),
        ("acme/team", "manifests", "acme/*/manifests", True),
        ("acme/team/sub", "manifests", "acme/*/manifests", False),
    ],
)
async def test_repository_allowlist_globs_do_not_cross_path_segments(owner, name, pattern, allowed):
    registry, _, secret_resolver = _registry(allowlists=[pattern])
    repository = GitRepository(provider="github", owner=owner, name=name)

    if allowed:
        provider = await registry.get_provider("github-main", repository)
        await provider.close()
        assert secret_resolver.calls == [("greenkube", "github-token", "token")]
    else:
        with pytest.raises(ProviderAuthorizationError):
            await registry.get_provider("github-main", repository)
        assert secret_resolver.calls == []


@pytest.mark.asyncio
@respx.mock
async def test_authorization_is_rechecked_before_each_provider_api_call():
    registry, provider_repository, _ = _registry(allowlists=["acme/*"])
    provider = await registry.get_provider("github-main", _repository())
    provider_repository.allowlists = ["other/*"]

    with pytest.raises(ProviderAuthorizationError):
        await provider.get_default_branch(_repository())

    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_authorized_provider_rechecks_repository_allowlist_for_archive_downloads():
    revision = "e" * 40
    api = f"https://api.github.test/repos/acme/manifests/tarball/{revision}"
    route = respx.get(api).mock(return_value=Response(200, content=b"archive"))
    registry, provider_repository, _ = _registry(allowlists=["acme/manifests"])
    repository = _repository()
    provider = await registry.get_provider("github-main", repository)

    assert await provider.get_archive(repository, revision, max_bytes=1024) == b"archive"
    provider_repository.allowlists = ["other/*"]
    with pytest.raises(ProviderAuthorizationError):
        await provider.get_archive(repository, revision, max_bytes=1024)

    assert route.call_count == 1
    assert provider_repository.allowlist_reads == 3
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_authorized_provider_delegates_ref_revision_after_allowlist_check():
    api_base = "https://api.github.test"
    sha = "a" * 40
    registry, _, _ = _registry(allowlists=["acme/manifests"])
    route = respx.get(f"{api_base}/repos/acme/manifests/git/ref/heads/main").mock(
        return_value=Response(200, json={"object": {"sha": sha}})
    )
    repository = GitRepository(
        provider="github",
        owner="acme",
        name="manifests",
        base_url=api_base,
        default_branch="main",
    )

    provider = await registry.get_provider("github-main", repository)
    assert await provider.get_ref_revision(repository, "main") == sha
    assert route.called
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_authorized_provider_forwards_pull_request_options_to_the_adapter():
    api = "https://api.github.test/repos/acme/manifests"
    create_route = respx.post(f"{api}/pulls").mock(
        return_value=Response(201, json={"number": 43, "html_url": "https://github.test/acme/manifests/pull/43"})
    )
    labels_route = respx.post(f"{api}/issues/43/labels").mock(return_value=Response(200, json=[]))
    reviewers_get_route = respx.get(f"{api}/pulls/43/requested_reviewers").mock(
        return_value=Response(200, json={"users": [], "teams": []})
    )
    reviewers_post_route = respx.post(f"{api}/pulls/43/requested_reviewers").mock(return_value=Response(201, json={}))
    registry, _, _ = _registry(allowlists=["acme/manifests"])
    repository = _repository()
    provider = await registry.get_provider("github-main", repository)
    options = PullRequestOptions(labels=("greenkube",), reviewers=("octocat",))

    pull_request = await provider.create_pull_request(
        repository,
        head="greenkube/plan-43",
        base="main",
        title="Tune workload",
        body="Rich plan body",
        options=options,
    )

    assert pull_request.number == 43
    assert json.loads(create_route.calls[0].request.content)["body"] == "Rich plan body"
    assert json.loads(labels_route.calls[0].request.content) == {"labels": ["greenkube"]}
    assert reviewers_get_route.called
    assert json.loads(reviewers_post_route.calls[0].request.content) == {
        "reviewers": ["octocat"],
        "team_reviewers": [],
    }
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_preflight_returns_capabilities_and_checks_auth_and_repository_read():
    respx.get("https://api.github.test/repos/acme/manifests").mock(
        return_value=Response(200, json={"default_branch": "main", "permissions": {"push": True}})
    )
    registry, _, _ = _registry(allowlists=["acme/manifests"])

    result = await registry.preflight("github-main", _repository(), required_capabilities={"multi_file_commit"})

    assert result.ready
    assert result.authenticated is True
    assert result.repository_readable is True
    assert result.pull_request_writable is True
    assert result.capabilities == provider_capabilities("github")
    assert result.missing_capabilities == frozenset()
    assert result.error_code is None
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_preflight_requires_capabilities_for_configured_pull_request_options():
    respx.get("https://api.github.test/repos/acme/manifests").mock(
        return_value=Response(200, json={"default_branch": "main", "permissions": {"push": True}})
    )
    registry, _, _ = _registry(allowlists=["acme/manifests"])
    options = PullRequestOptions(
        draft=True,
        labels=("greenkube",),
        reviewers=("octocat",),
        team_reviewers=("platform",),
    )

    result = await registry.preflight(
        "github-main",
        _repository(),
        pull_request_options=options,
    )

    assert result.ready
    assert result.required_capabilities == frozenset(
        {
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.TEAM_REVIEWERS,
        }
    )
    assert result.missing_capabilities == frozenset()
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_preflight_checks_nested_project_write_permission():
    respx.get("https://gitlab.test/api/v4/projects/group%2Fsubgroup%2Fmanifests").mock(
        return_value=Response(200, json={"permissions": {"project_access": {"access_level": 30}}})
    )
    project = GitRepository(provider="gitlab", owner="group/subgroup", name="manifests")
    registry, _, _ = _registry(
        _config(provider_type="gitlab", api_base_url="https://gitlab.test"),
        allowlists=["group/subgroup/manifests"],
    )

    result = await registry.preflight("github-main", project)

    assert result.ready
    assert result.pull_request_writable is True
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_gitea_preflight_checks_push_permission():
    respx.get("https://gitea.test/api/v1/repos/acme/manifests").mock(
        return_value=Response(200, json={"permissions": {"push": True}})
    )
    registry, _, _ = _registry(
        _config(provider_type="gitea", api_base_url="https://gitea.test"),
        allowlists=["acme/manifests"],
    )

    result = await registry.preflight("github-main", _repository_for_provider("gitea"))

    assert result.ready
    assert result.pull_request_writable is True
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_preflight_fails_closed_when_repository_is_not_writable():
    respx.get("https://api.github.test/repos/acme/manifests").mock(
        return_value=Response(200, json={"permissions": {"push": False}})
    )
    registry, _, _ = _registry(allowlists=["acme/manifests"])

    result = await registry.preflight("github-main", _repository())

    assert not result.ready
    assert result.authenticated is True
    assert result.repository_readable is True
    assert result.pull_request_writable is False
    assert result.error_code == "repository-not-writable"
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_preflight_fails_closed_when_write_permission_is_not_reported():
    respx.get("https://api.github.test/repos/acme/manifests").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    registry, _, _ = _registry(allowlists=["acme/manifests"])

    result = await registry.preflight("github-main", _repository())

    assert not result.ready
    assert result.repository_readable is True
    assert result.pull_request_writable is None
    assert result.error_code == "pull-request-write-permission-unknown"


@pytest.mark.asyncio
@respx.mock
async def test_preflight_reports_missing_capabilities_without_git_api_call():
    registry, _, _ = _registry(allowlists=["acme/manifests"])

    result = await registry.preflight("github-main", _repository(), required_capabilities={"branch_protection_check"})

    assert not result.ready
    assert result.authenticated is None
    assert result.repository_readable is None
    assert result.missing_capabilities == frozenset({GitProviderCapability.BRANCH_PROTECTION_CHECK})
    assert result.error_code == "provider-capability-missing"
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "options", "missing"),
    [
        ("gitlab", PullRequestOptions(team_reviewers=("platform",)), GitProviderCapability.TEAM_REVIEWERS),
        ("gitea", PullRequestOptions(draft=True), GitProviderCapability.DRAFT_PULL_REQUEST),
    ],
)
@respx.mock
async def test_preflight_rejects_provider_specific_pr_options_without_git_api_call(provider_type, options, missing):
    base_url = "https://gitlab.test" if provider_type == "gitlab" else "http://gitea.test"
    repository = _repository_for_provider(provider_type)
    registry, _, _ = _registry(
        _config(provider_type=provider_type, api_base_url=base_url),
        allowlists=["acme/manifests"],
    )

    result = await registry.preflight(
        "github-main",
        repository,
        pull_request_options=options,
    )

    assert not result.ready
    assert result.missing_capabilities == frozenset({missing})
    assert result.error_code == "provider-capability-missing"
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_preflight_rejects_malformed_gitlab_reviewer_ids_without_git_api_call():
    registry, _, _ = _registry(
        _config(provider_type="gitlab", api_base_url="https://gitlab.test"),
        allowlists=["acme/manifests"],
    )

    result = await registry.preflight(
        "github-main",
        _repository_for_provider("gitlab"),
        pull_request_options=PullRequestOptions(reviewers=("octocat",)),
    )

    assert not result.ready
    assert result.missing_capabilities == frozenset()
    assert result.error_code == "invalid-pull-request-options"
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_preflight_authentication_failure_has_a_safe_error_code():
    respx.get("https://api.github.test/repos/acme/manifests").mock(return_value=Response(401, json={"message": TOKEN}))
    registry, _, _ = _registry(allowlists=["acme/manifests"])

    result = await registry.preflight("github-main", _repository())

    assert not result.ready
    assert result.authenticated is False
    assert result.repository_readable is None
    assert result.error_code == "provider-authentication-failed"
    assert TOKEN not in repr(result)
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_provider_errors_redact_token_material():
    respx.get("https://api.github.test/repos/acme/manifests").mock(return_value=Response(401, json={"message": TOKEN}))
    registry, _, _ = _registry(allowlists=["acme/manifests"])
    provider = await registry.get_provider("github-main", _repository())

    with pytest.raises(GitProviderError) as error:
        await provider.get_default_branch(_repository())

    assert TOKEN not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.asyncio
@respx.mock
async def test_unauthorized_preflight_does_not_make_a_provider_api_call():
    registry, _, secret_resolver = _registry(allowlists=["other/*"])

    with pytest.raises(ProviderAuthorizationError):
        await registry.preflight("github-main", _repository())

    assert secret_resolver.calls == []
    assert len(respx.calls) == 0
