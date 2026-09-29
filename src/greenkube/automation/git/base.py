# src/greenkube/automation/git/base.py
"""Git provider abstraction used by the recommendation PR bot.

The automation service never talks to a specific Git platform directly: it
calls a :class:`GitProvider` adapter, so GitHub, GitLab and self-hosted Gitea
share the same orchestration. No knowledge of ArgoCD/Flux lives here — the bot
only locates manifests through workload annotations and edits YAML.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)


class GitProviderError(RuntimeError):
    """Raised when a Git provider call fails with a user-actionable message."""


class GitProviderTransientError(GitProviderError):
    """A provider failure which may succeed when retried."""


# Characters and sequences that Git itself forbids in ref names. They are also
# the building blocks of URL/path injection, so both are rejected up front.
_GIT_REF_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\\]")
_GIT_REF_FORBIDDEN_SEQUENCES = ("..", "@{", "//")


def validate_git_ref(ref: str, *, field: str = "git ref") -> str:
    """Validate a branch or ref name and prevent URL/path injection.

    Raises :class:`GitProviderError` for names that are not valid Git refs
    (e.g. ``../main``, ``feature..x``, ``branch name``).
    """
    value = (ref or "").strip()
    if not value or len(value) > 255:
        raise GitProviderError(f"Invalid {field}: must be 1-255 characters.")
    if _GIT_REF_FORBIDDEN_CHARS.search(value):
        raise GitProviderError(f"Invalid {field} '{value}': contains forbidden characters.")
    if any(seq in value for seq in _GIT_REF_FORBIDDEN_SEQUENCES):
        raise GitProviderError(f"Invalid {field} '{value}': contains a forbidden sequence.")
    if value.startswith(("/", ".", "-")) or value.endswith(("/", ".", ".lock")):
        raise GitProviderError(f"Invalid {field} '{value}': invalid start or end.")
    return value


def quote_git_path(path: str) -> str:
    """Percent-encode a repository path for URL use, preserving ``/`` separators."""
    return quote(path.lstrip("/"), safe="/")


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
    validate_provider: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.validate_provider and self.provider not in ("github", "gitlab", "gitea"):
            raise GitProviderError(f"Unsupported Git provider '{self.provider}'.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.provider):
            raise GitProviderError("Invalid Git provider name.")
        components = [*self.owner.split("/"), self.name]
        if not components or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in components):
            raise GitProviderError("Invalid repository owner or name.")
        if any(part in (".", "..") for part in components):
            raise GitProviderError("Invalid repository owner or name.")

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
    if any(ch in value for ch in "\r\n?#"):
        raise GitProviderError("Repository URL contains invalid query, fragment, or control characters.")
    if value.startswith("git@"):
        # git@host:owner/repo(.git)
        _, _, path = value.partition(":")
    else:
        # Strip scheme and host.
        value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
        # Drop credentials if present (https://user:token@host/...)
        value = value.split("@", 1)[-1]
        # Test/configuration redaction sometimes leaves the credential marker
        # in front of an otherwise valid host.
        normalized_url = url.replace("******", "", 1) if url.startswith("******") else url
        parsed = urlsplit(normalized_url if "://" in normalized_url else f"https://{normalized_url}")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise GitProviderError(f"Could not parse a repository from URL: {url}")
        path = parsed.path

    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise GitProviderError(f"Could not parse a repository from URL: {url}")

    if any(p in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", p) for p in parts):
        raise GitProviderError(f"Invalid repository path in URL: {url}")
    if provider == "gitlab":
        return "/".join(parts[:-1]), parts[-1]
    owner = "/".join(parts[:-1])
    if provider in ("github", "gitea") and len(parts) != 2:
        raise GitProviderError(f"Invalid repository path for {provider}: {url}")
    return owner, parts[-1]


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
            validate_provider=self.name in ("github", "gitlab", "gitea"),
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
        for attempt in range(3):
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < 2:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise GitProviderTransientError(f"{self.name} transient request failure: {exc}") from exc
            except httpx.HTTPError as exc:
                raise GitProviderError(f"{self.name} request failed: {exc}") from exc
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
                continue
            break

        if response.status_code == 404:
            return response
        if response.status_code not in ok_statuses:
            detail = _error_detail(response)
            if response.status_code in (429, 500, 502, 503, 504):
                raise GitProviderTransientError(
                    f"{self.name} transient API error {response.status_code} on {method} {path}: {detail}"
                )
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
    ) -> "PullRequestInfo": ...

    @abstractmethod
    async def get_pull_request(self, repo: GitRepository, number: int) -> "PullRequestInfo": ...

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

    # Keep compatibility with the service's existing mapping-style access
    # while exposing a typed result to callers.
    def get(self, key: str, default=None):
        return getattr(self, {"html_url": "url", "web_url": "url", "state": "status"}.get(key, key), default)

    def __getitem__(self, key: str):
        value = self.get(key)
        if value is None and key not in ("merged",):
            raise KeyError(key)
        return value


def normalize_pull_request(data: dict, *, repo: GitRepository) -> PullRequestInfo:
    """Validate and normalize provider-specific pull-request payloads."""
    if not isinstance(data, dict):
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request payload.")
    number = data.get("number", data.get("iid"))
    url = data.get("html_url", data.get("web_url"))
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request number.")
    if not isinstance(url, str) or any(ord(c) < 32 or c.isspace() for c in url):
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request URL.")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request URL.")
    if not parsed.path.strip("/"):
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request URL.")
    status = str(data.get("state", "unknown"))
    merged = bool(data.get("merged") or data.get("merged_at") or status == "merged")
    return PullRequestInfo(number=number, url=url, status=status, merged=merged, raw=data)
