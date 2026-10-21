from dataclasses import dataclass

import pytest

from greenkube.automation.git.base import GitProviderError, GitRepository, PullRequestInfo
from greenkube.automation.pr_lifecycle_runtime import RegistryPullRequestLifecycleProvider
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig


class _ProviderRepository:
    def __init__(self, provider: GitOpsProviderConfig | None) -> None:
        self.provider = provider

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return self.provider if self.provider is not None and self.provider.id == provider_id else None


@dataclass
class _FakeProvider:
    response: PullRequestInfo
    closed: bool = False
    requested: tuple[GitRepository, int] | None = None

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        self.requested = (repo, number)
        return self.response

    async def close(self) -> None:
        self.closed = True


class _Registry:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider
        self.provider_id: str | None = None
        self.repository: GitRepository | None = None

    async def get_provider(self, provider_id: str, repository: GitRepository) -> _FakeProvider:
        self.provider_id = provider_id
        self.repository = repository
        return self.provider


@pytest.mark.asyncio
async def test_lifecycle_provider_refetches_allowlisted_provider_pr_and_closes_client():
    provider = _FakeProvider(
        PullRequestInfo(
            number=23,
            url="https://github.com/acme/platform/pull/23",
            status="closed",
            merged=True,
            raw={
                "number": 23,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": "merge-sha",
                "repository": {"full_name": "acme/platform"},
            },
        )
    )
    registry = _Registry(provider)
    resolver = RegistryPullRequestLifecycleProvider(
        _ProviderRepository(
            GitOpsProviderConfig(
                id="github-main",
                provider_type="github",
                api_base_url="https://api.github.test",
                secret_name="git-token",
                secret_key="token",
            )
        ),
        registry,
    )

    result = await resolver.get_pull_request("github-main", "acme/platform", 23)

    assert result is provider.response
    assert registry.provider_id == "github-main"
    assert registry.repository == GitRepository(
        provider="github",
        owner="acme",
        name="platform",
        base_url="https://api.github.test",
    )
    assert provider.requested == (registry.repository, 23)
    assert provider.closed


@pytest.mark.asyncio
async def test_lifecycle_provider_rejects_invalid_provider_repository_before_registry():
    provider = _FakeProvider(
        PullRequestInfo(
            number=23,
            url="https://github.com/acme/platform/pull/23",
            status="closed",
            merged=True,
        )
    )
    registry = _Registry(provider)
    resolver = RegistryPullRequestLifecycleProvider(
        _ProviderRepository(
            GitOpsProviderConfig(
                id="github-main",
                provider_type="github",
                secret_name="git-token",
                secret_key="token",
            )
        ),
        registry,
    )

    with pytest.raises(GitProviderError):
        await resolver.get_pull_request("github-main", "acme/nested/platform", 23)

    assert registry.repository is None
    assert not provider.closed


@pytest.mark.asyncio
async def test_lifecycle_provider_skips_missing_or_disabled_provider():
    provider = _FakeProvider(
        PullRequestInfo(
            number=23,
            url="https://github.com/acme/platform/pull/23",
            status="closed",
            merged=True,
        )
    )
    registry = _Registry(provider)
    resolver = RegistryPullRequestLifecycleProvider(
        _ProviderRepository(
            GitOpsProviderConfig(
                id="github-main",
                provider_type="github",
                secret_name="git-token",
                secret_key="token",
                enabled=False,
            )
        ),
        registry,
    )

    assert await resolver.get_pull_request("github-main", "acme/platform", 23) is None
    assert registry.repository is None
    assert not provider.closed
