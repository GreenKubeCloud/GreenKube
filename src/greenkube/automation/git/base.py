# src/greenkube/automation/git/base.py
"""Git provider abstraction used by the recommendation PR bot.

The automation service never talks to a specific Git platform directly: it
calls a :class:`GitProvider` adapter, so GitHub, GitLab and self-hosted Gitea
share the same orchestration. No knowledge of ArgoCD/Flux lives here — the bot
only locates manifests through workload annotations and edits YAML.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


class GitProviderError(RuntimeError):
    """Raised when a Git provider call fails with a user-actionable message."""


@dataclass
class GitFile:
    """A versioned file returned by the provider."""

    path: str
    content: str
    sha: Optional[str] = None


@dataclass
class GitRepository:
    """A repository target in ``owner/name`` form."""

    provider: str
    owner: str
    name: str
    base_url: Optional[str] = None
    default_branch: str = "main"

    @property
    def full_name(self) -> str:
        """Returns the provider-side repository identifier."""
        return f"{self.owner}/{self.name}"


def parse_repo_url(url: str, provider: str = "github") -> tuple[str, str]:
    """Parses a Git remote URL into ``(owner, name)``.

    Supports HTTPS and SSH remotes for SaaS and self-hosted instances:
    ``https://github.com/owner/repo.git``, ``git@git.example.com:group/repo.git``.
    GitLab nested groups are preserved in the owner segment.
    """
    if not url:
        raise GitProviderError("Repository URL is empty.")

    value = url.strip()
    if value.startswith("git@"):
        # git@host:owner/repo(.git)
        _, _, path = value.partition(":")
    else:
        # Strip scheme and host.
        value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
        # Drop credentials if present (https://user:token@host/...)
        value = value.split("@", 1)[-1]
        path = value.split("/", 1)[1] if "/" in value else value

    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise GitProviderError(f"Could not parse a repository from URL: {url}")

    if provider == "gitlab":
        return "/".join(parts[:-1]), parts[-1]
    return "/".join(parts[:-2]) if len(parts) > 2 else parts[-2], parts[-1]


class GitProvider(ABC):
    """Adapter contract for a Git platform."""

    name: str = "git"

    def __init__(
        self,
        token: str,
        *,
        api_base_url: Optional[str] = None,
        author_name: str = "GreenKube Bot",
        author_email: str = "bot@greenkube.cloud",
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.token = token
        self.api_base_url = (api_base_url or "").rstrip("/")
        self.author_name = author_name
        self.author_email = author_email
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    def repository(self, url: str, base_branch: Optional[str] = None) -> GitRepository:
        """Builds the repository target from a remote URL."""
        owner, name = parse_repo_url(url, provider=self.name)
        return GitRepository(
            provider=self.name,
            owner=owner,
            name=name,
            base_url=self.api_base_url or None,
            default_branch=base_branch or "main",
        )

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        ok_statuses: tuple = (200, 201, 204),
    ) -> httpx.Response:
        client = await self._client_or_create()
        url = f"{self.api_base_url}{path}" if self.api_base_url else path
        try:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise GitProviderError(f"{self.name} request failed: {exc}") from exc

        if response.status_code == 404:
            return response
        if response.status_code not in ok_statuses:
            detail = _error_detail(response)
            raise GitProviderError(f"{self.name} API error {response.status_code} on {method} {path}: {detail}")
        return response

    def _headers(self) -> dict:
        return {"Accept": "application/json"}

    async def close(self) -> None:
        """Closes an internally created HTTP client."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_default_branch(self, repo: GitRepository) -> str: ...

    @abstractmethod
    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]: ...

    @abstractmethod
    async def list_files(self, repo: GitRepository, ref: str) -> List[str]: ...

    @abstractmethod
    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None: ...

    @abstractmethod
    async def update_file(
        self,
        repo: GitRepository,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> None: ...

    @abstractmethod
    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict: ...

    @abstractmethod
    async def get_pull_request(self, repo: GitRepository, number: int) -> dict: ...

    async def test_connection(self) -> bool:
        """Checks that the provider is reachable and the token is valid."""
        try:
            await self._request("GET", "/user")
            return True
        except GitProviderError:
            return False


def _error_detail(response: httpx.Response) -> str:
    """Extracts a compact error message from a provider response."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        for key in ("message", "error", "error_description"):
            if data.get(key):
                return str(data[key])[:300]
    return str(data)[:300]


@dataclass
class PullRequestInfo:
    """Normalized pull-request information across providers."""

    number: int
    url: str
    status: str
    merged: bool = False
    raw: dict = field(default_factory=dict)
