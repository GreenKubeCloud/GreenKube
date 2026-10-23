"""Registry for Secret-backed, repository-scoped Git provider adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import quote

from greenkube.automation.git.base import (
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitProvider,
    GitProviderArchiveLimitError,
    GitProviderCapability,
    GitProviderCapabilityError,
    GitProviderError,
    GitProviderTransientError,
    GitRepository,
    PullRequestInfo,
    PullRequestOptions,
    normalize_git_provider_capabilities,
    pull_request_option_capabilities,
    repository_matches_allowlist,
)
from greenkube.automation.git.credentials import (
    GitProviderCredentialResolver,
    KubernetesSecretResolver,
)
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsProviderRepository

_PROVIDER_ADAPTERS: dict[str, type[GitProvider]] = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "gitea": GiteaProvider,
}
_RESULT = TypeVar("_RESULT")


class GitProviderRegistryError(GitProviderError):
    """Base class for registry configuration errors."""


class UnknownGitProviderError(GitProviderRegistryError):
    """Raised when the requested provider configuration or adapter is unknown."""

    code = "unknown-git-provider"


class GitProviderConfigurationError(GitProviderRegistryError):
    """Raised when a configured provider cannot be used."""

    code = "git-provider-unavailable"


class ProviderAuthorizationError(GitProviderRegistryError):
    """Raised when a repository is not authorized for a provider."""

    code = "repository-not-allowed"


@dataclass(frozen=True, slots=True)
class GitProviderPreflightResult:
    """Safe summary of authentication, repository permissions and capabilities."""

    provider_id: str
    provider_type: str
    repository: str
    capabilities: frozenset[GitProviderCapability | str]
    required_capabilities: frozenset[GitProviderCapability | str]
    missing_capabilities: frozenset[GitProviderCapability | str]
    authenticated: bool | None
    repository_readable: bool | None
    pull_request_writable: bool | None = None
    error_code: str | None = None

    @property
    def ready(self) -> bool:
        """Whether all checks represented by this result passed."""
        return (
            not self.missing_capabilities
            and self.authenticated is True
            and self.repository_readable is True
            and self.pull_request_writable is True
            and self.error_code is None
        )


def provider_capabilities(provider_type: str) -> frozenset[GitProviderCapability]:
    """Return the adapter's declared capabilities without creating a provider."""
    normalized_type, adapter = _provider_adapter(provider_type)
    del normalized_type
    return frozenset(adapter.capabilities)


class GitProviderRegistry:
    """Construct Git adapters only after provider and repository authorization."""

    def __init__(
        self,
        provider_repository: GitOpsProviderRepository,
        secret_resolver: KubernetesSecretResolver,
        *,
        author_name: str = "GreenKube Bot",
        author_email: str = "bot@greenkube.cloud",
    ):
        self._provider_repository = provider_repository
        self._credential_resolver = GitProviderCredentialResolver(secret_resolver)
        self._author_name = author_name
        self._author_email = author_email

    async def get_provider(self, provider_id: str, repository: GitRepository) -> _AuthorizedGitProvider:
        """Create a provider scoped to one allowlisted repository.

        This method does not make Git API calls. The returned adapter rechecks
        the current allowlist before each operation that can call the provider.
        """
        config, provider_type, adapter = await self._load_provider_config(provider_id)
        await self._authorize_repository(config, provider_type, repository)
        token = await self._credential_resolver.resolve(config)
        provider = adapter(
            token,
            api_base_url=config.api_base_url or None,
            author_name=self._author_name,
            author_email=self._author_email,
        )

        async def authorize(target: GitRepository) -> None:
            current_config, current_type, _ = await self._load_provider_config(provider_id)
            if current_type != provider_type or current_config.id != config.id:
                raise GitProviderConfigurationError("Git provider configuration changed; create a new provider.")
            await self._authorize_repository(current_config, current_type, target)

        return _AuthorizedGitProvider(provider, repository, authorize)

    async def preflight(
        self,
        provider_id: str,
        repository: GitRepository,
        *,
        required_capabilities: Iterable[GitProviderCapability | str] = (),
        pull_request_options: PullRequestOptions | None = None,
    ) -> GitProviderPreflightResult:
        """Check credentials, authentication, repository read and PR write access, and capabilities.

        Configuration, Secret resolution and allowlist failures are raised as
        typed errors. Provider API failures are represented by safe error codes;
        provider response text and credential material are never included.
        """
        provider = await self.get_provider(provider_id, repository)
        capabilities = normalize_git_provider_capabilities(provider.capabilities)
        required = normalize_git_provider_capabilities(required_capabilities)
        if pull_request_options is not None:
            required |= pull_request_option_capabilities(pull_request_options)
        missing = required - capabilities
        try:
            if missing:
                return GitProviderPreflightResult(
                    provider_id=provider_id,
                    provider_type=provider.name,
                    repository=repository.full_name,
                    capabilities=capabilities,
                    required_capabilities=required,
                    missing_capabilities=missing,
                    authenticated=None,
                    repository_readable=None,
                    error_code=GitProviderCapabilityError.code,
                )

            if pull_request_options is not None:
                try:
                    provider.validate_pull_request_options(pull_request_options)
                except GitProviderError:
                    return GitProviderPreflightResult(
                        provider_id=provider_id,
                        provider_type=provider.name,
                        repository=repository.full_name,
                        capabilities=capabilities,
                        required_capabilities=required,
                        missing_capabilities=missing,
                        authenticated=None,
                        repository_readable=None,
                        error_code="invalid-pull-request-options",
                    )

            try:
                repository_info = await provider._preflight_repository(repository)
            except GitProviderError as error:
                authenticated = False if error.status_code == 401 else True if error.status_code == 403 else None
                return GitProviderPreflightResult(
                    provider_id=provider_id,
                    provider_type=provider.name,
                    repository=repository.full_name,
                    capabilities=capabilities,
                    required_capabilities=required,
                    missing_capabilities=missing,
                    authenticated=authenticated,
                    repository_readable=False if error.status_code == 403 else None,
                    error_code=(
                        "provider-authentication-failed" if error.status_code == 401 else "repository-read-failed"
                    ),
                )

            if repository_info is None:
                return GitProviderPreflightResult(
                    provider_id=provider_id,
                    provider_type=provider.name,
                    repository=repository.full_name,
                    capabilities=capabilities,
                    required_capabilities=required,
                    missing_capabilities=missing,
                    authenticated=None,
                    repository_readable=False,
                    error_code="repository-read-failed",
                )

            writable = _pull_request_write_permission(provider.name, repository_info)
            return GitProviderPreflightResult(
                provider_id=provider_id,
                provider_type=provider.name,
                repository=repository.full_name,
                capabilities=capabilities,
                required_capabilities=required,
                missing_capabilities=missing,
                authenticated=True,
                repository_readable=True,
                pull_request_writable=writable,
                error_code=(
                    None
                    if writable is True
                    else "repository-not-writable"
                    if writable is False
                    else "pull-request-write-permission-unknown"
                ),
            )
        finally:
            await provider.close()

    async def _load_provider_config(
        self,
        provider_id: str,
    ) -> tuple[GitOpsProviderConfig, str, type[GitProvider]]:
        if not isinstance(provider_id, str) or not provider_id.strip() or provider_id != provider_id.strip():
            raise UnknownGitProviderError("Git provider configuration was not found.")
        config = await self._provider_repository.get_provider_config(provider_id)
        if config is None:
            raise UnknownGitProviderError("Git provider configuration was not found.")
        provider_type, adapter = _provider_adapter(config.provider_type)
        if not config.enabled:
            raise GitProviderConfigurationError("Git provider is disabled.")
        return config, provider_type, adapter

    async def _authorize_repository(
        self,
        config: GitOpsProviderConfig,
        provider_type: str,
        repository: GitRepository,
    ) -> None:
        if not isinstance(repository, GitRepository) or repository.provider != provider_type:
            raise ProviderAuthorizationError("Repository provider does not match the configured Git provider.")
        patterns = await self._provider_repository.list_provider_allowlists(config.id)
        if not patterns:
            raise ProviderAuthorizationError("Git provider has an empty repository allowlist; access is denied.")
        if not any(
            isinstance(pattern, str) and pattern and repository_matches_allowlist(repository.full_name, pattern)
            for pattern in patterns
        ):
            raise ProviderAuthorizationError("Repository is not allowed by the Git provider allowlist.")


class _AuthorizedGitProvider(GitProvider):
    """Adapter proxy that rechecks the allowlist before every Git API operation."""

    def __init__(
        self,
        provider: GitProvider,
        repository: GitRepository,
        authorize: Callable[[GitRepository], Awaitable[None]],
    ):
        self._provider = provider
        self._repository = repository
        self._authorize = authorize
        self.name = provider.name
        self.capabilities = provider.capabilities
        self.api_base_url = provider.api_base_url
        self.author_name = provider.author_name
        self.author_email = provider.author_email
        self.timeout = provider.timeout
        self._client = None
        self._owns_client = False

    def repository(self, url: str, base_branch: str | None = None) -> GitRepository:
        return self._provider.repository(url, base_branch)

    def validate_pull_request_options(self, options: PullRequestOptions | None = None) -> PullRequestOptions:
        return self._provider.validate_pull_request_options(options)

    async def _call(
        self,
        repository: GitRepository,
        operation: Callable[[], Awaitable[_RESULT]],
    ) -> _RESULT:
        await self._authorize(repository)
        try:
            return await operation()
        except GitProviderError as error:
            raise _redact_provider_error(error, self._provider.token) from None

    async def get_default_branch(self, repo: GitRepository) -> str:
        return await self._call(repo, lambda: self._provider.get_default_branch(repo))

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
        return await self._call(repo, lambda: self._provider.get_ref_revision(repo, ref))

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> GitFile | None:
        return await self._call(repo, lambda: self._provider.get_file(repo, path, ref))

    async def list_files(self, repo: GitRepository, ref: str) -> list[str]:
        return await self._call(repo, lambda: self._provider.list_files(repo, ref))

    async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
        return await self._call(
            repo,
            lambda: self._provider.get_archive(repo, revision, max_bytes=max_bytes),
        )

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        await self._call(repo, lambda: self._provider.create_branch(repo, branch, from_ref))

    async def update_file(
        self,
        repo: GitRepository,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        await self._call(
            repo,
            lambda: self._provider.update_file(repo, path, content, message, branch, sha),
        )

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        return await self._call(request.repo, lambda: self._provider.create_commit(request))

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo:
        return await self._call(
            repo,
            lambda: self._provider.create_pull_request(
                repo,
                head=head,
                base=base,
                title=title,
                body=body,
                options=options,
            ),
        )

    async def ensure_pull_request_options(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        options: PullRequestOptions | None = None,
    ) -> None:
        await self._call(
            repo,
            lambda: self._provider.ensure_pull_request_options(repo, pull_request, options),
        )

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> PullRequestInfo | None:
        return await self._call(
            repo,
            lambda: self._provider.find_pull_request(repo, head=head, base=base),
        )

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        return await self._call(repo, lambda: self._provider.get_pull_request(repo, number))

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> PullRequestInfo | None:
        return await self._call(
            repo,
            lambda: self._provider.find_open_pull_request_for_branch(repo, branch),
        )

    async def delete_plan_branch(self, repo: GitRepository, branch: str) -> None:
        await self._call(repo, lambda: self._provider.delete_plan_branch(repo, branch))

    async def test_connection(self) -> bool:
        try:
            response = await self._call(
                self._repository,
                lambda: self._provider._request("GET", "/user"),
            )
        except GitProviderError:
            return False
        return response.status_code == 200

    async def _preflight_repository(self, repository: GitRepository) -> dict[str, object] | None:
        if self.name == "gitlab":
            path = f"/projects/{quote(repository.full_name, safe='')}"
        else:
            path = f"/repos/{repository.owner}/{repository.name}"
        response = await self._call(repository, lambda: self._provider._request("GET", path))
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            return None
        return data

    async def close(self) -> None:
        await self._provider.close()


def _provider_adapter(provider_type: str) -> tuple[str, type[GitProvider]]:
    normalized_type = provider_type.strip().lower() if isinstance(provider_type, str) else ""
    adapter = _PROVIDER_ADAPTERS.get(normalized_type)
    if adapter is None:
        raise UnknownGitProviderError("Configured Git provider type is not supported.")
    return normalized_type, adapter


def _redact_provider_error(error: GitProviderError, token: str) -> GitProviderError:
    message = str(error).replace(token, "[REDACTED]") if token else str(error)
    if isinstance(error, GitProviderCapabilityError):
        reason = error.reason.replace(token, "[REDACTED]") if token and error.reason else error.reason
        return GitProviderCapabilityError(error.provider, error.capability, reason=reason)
    if isinstance(error, GitProviderArchiveLimitError):
        return GitProviderArchiveLimitError(message, status_code=error.status_code)
    if isinstance(error, GitConflictError):
        return GitConflictError(message, status_code=error.status_code)
    if isinstance(error, GitProviderTransientError):
        return GitProviderTransientError(message, status_code=error.status_code)
    return GitProviderError(message, status_code=error.status_code)


def _pull_request_write_permission(provider_type: str, repository_info: dict[str, object]) -> bool | None:
    permissions = repository_info.get("permissions")
    if not isinstance(permissions, dict):
        return None
    if provider_type == "gitlab":
        access_levels = []
        for access_name in ("project_access", "group_access"):
            access = permissions.get(access_name)
            if isinstance(access, dict):
                level = access.get("access_level")
                if isinstance(level, int) and not isinstance(level, bool):
                    access_levels.append(level)
        return max(access_levels) >= 30 if access_levels else None

    permission_names = (
        ("push", "maintain", "admin", "owner")
        if provider_type == "gitea"
        else (
            "push",
            "maintain",
            "admin",
        )
    )
    flags = [permissions.get(name) for name in permission_names if isinstance(permissions.get(name), bool)]
    return any(flags) if flags else None
