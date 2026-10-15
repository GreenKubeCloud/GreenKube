"""Authorized Git provider adapters for pull-request lifecycle reconciliation."""

from __future__ import annotations

import re
from typing import Protocol, cast

from greenkube.automation.git.base import GitProviderError, GitRepository, PullRequestInfo
from greenkube.automation.plan_worker import PlanWorker, RegistryPlanProviderResolver
from greenkube.automation.pr_lifecycle import (
    PullRequestLifecyclePlanRepository,
    PullRequestLifecycleReconciler,
    PullRequestLifecycleRepository,
)
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig

_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_SUPPORTED_PROVIDERS = frozenset({"github", "gitlab", "gitea"})


class _AuthorizedPullRequestProvider(Protocol):
    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo: ...

    async def close(self) -> None: ...


class _AuthorizedProviderRegistry(Protocol):
    async def get_provider(self, provider_id: str, repository: GitRepository) -> _AuthorizedPullRequestProvider: ...


class _ProviderConfigLookup(Protocol):
    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None: ...


class _LifecycleProviderResolver(Protocol):
    provider_repository: _ProviderConfigLookup
    registry: _AuthorizedProviderRegistry


class RegistryPullRequestLifecycleProvider:
    """Re-fetch provider state only after registry allowlist authorization."""

    def __init__(
        self,
        provider_repository: _ProviderConfigLookup,
        registry: _AuthorizedProviderRegistry,
    ) -> None:
        self._provider_repository = provider_repository
        self._registry = registry

    async def get_pull_request(self, provider_id: str, repo: str, number: int) -> PullRequestInfo | None:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")

        provider = await self._provider_repository.get_provider_config(provider_id)
        if provider is None or not provider.enabled:
            return None
        repository = _provider_repository(provider, repo)
        client = await self._registry.get_provider(provider_id, repository)
        try:
            return await client.get_pull_request(repository, number)
        finally:
            await client.close()


def create_pull_request_lifecycle_reconciler(worker: PlanWorker) -> PullRequestLifecycleReconciler:
    """Compose lifecycle reconciliation from the existing authorized plan worker."""
    resolver = cast(_LifecycleProviderResolver, cast(RegistryPlanProviderResolver, worker.provider_resolver))
    return PullRequestLifecycleReconciler(
        pull_request_repository=cast(PullRequestLifecycleRepository, worker.pull_request_repository),
        plan_repository=cast(PullRequestLifecyclePlanRepository, worker.plans),
        git_provider=RegistryPullRequestLifecycleProvider(resolver.provider_repository, resolver.registry),
    )


def _provider_repository(provider: GitOpsProviderConfig, full_name: str) -> GitRepository:
    provider_type = provider.provider_type.strip().lower()
    if provider_type not in _SUPPORTED_PROVIDERS:
        raise GitProviderError("Configured Git provider type is not supported for pull-request reconciliation.")
    if not isinstance(full_name, str) or full_name != full_name.strip():
        raise GitProviderError("Provider pull-request repository identifier is invalid.")
    parts = full_name.split("/")
    if (
        len(parts) < 2
        or (provider_type in {"github", "gitea"} and len(parts) != 2)
        or any(part in {".", ".."} or _REPOSITORY_COMPONENT.fullmatch(part) is None for part in parts)
    ):
        raise GitProviderError("Provider pull-request repository identifier is invalid.")
    return GitRepository(
        provider=provider_type,
        owner="/".join(parts[:-1]),
        name=parts[-1],
        base_url=provider.api_base_url,
    )
