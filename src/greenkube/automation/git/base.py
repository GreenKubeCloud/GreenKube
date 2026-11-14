# src/greenkube/automation/git/base.py
"""Git provider abstraction used by the recommendation PR bot.

The automation service never talks to a specific Git platform directly: it
calls a :class:`GitProvider` adapter, so GitHub, GitLab and self-hosted Gitea
share the same orchestration. No knowledge of ArgoCD/Flux lives here — the bot
only locates manifests through workload annotations and edits YAML.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable, List, Literal, Optional
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, field_validator

from greenkube.automation.git.budget import charge_provider_call
from greenkube.automation.git.rate_limit import (
    MAX_INLINE_RATE_LIMIT_WAIT_SECONDS,
    announced_wait_seconds,
    rate_limit_retry_at,
    record_rate_limited,
)
from greenkube.gitops.sha import GIT_OBJECT_SHA
from greenkube.gitops.sha import is_full_commit_sha as is_full_commit_sha  # re-exported for the adapters

logger = logging.getLogger(__name__)

_IMMUTABLE_CACHE_SIZE = 64


class GitProviderCapability(StrEnum):
    """Features that a Git provider adapter can guarantee."""

    MULTI_FILE_COMMIT = "multi_file_commit"
    FIND_PULL_REQUEST = "find_pull_request"
    GET_REF_REVISION = "get_ref_revision"
    REPOSITORY_ARCHIVE = "repository_archive"
    COMPLETE_FILE_TREE = "complete_file_tree"
    DRAFT_PULL_REQUEST = "draft_pull_request"
    LABELS = "labels"
    REVIEWERS = "reviewers"
    TEAM_REVIEWERS = "team_reviewers"
    WEBHOOKS = "webhooks"
    MERGE_DETECTION = "merge_detection"
    BRANCH_PROTECTION_CHECK = "branch_protection_check"
    FIND_OPEN_PULL_REQUEST_FOR_BRANCH = "find_open_pull_request_for_branch"
    DELETE_PLAN_BRANCH = "delete_plan_branch"
    COMMIT_ANCESTRY = "commit_ancestry"
    #: Commit statuses on a PR head (``greenkube/validation``, J5).
    COMMIT_STATUS = "commit_status"
    #: Create and edit PR comments (the lifecycle comment, J5).
    PULL_REQUEST_COMMENTS = "pull_request_comments"
    #: Add labels and reviewers to an existing PR (derived metadata, J2/J3).
    PULL_REQUEST_METADATA = "pull_request_metadata"


#: Commit status states accepted by :meth:`GitProvider.set_commit_status`.
CommitStatusState = Literal["pending", "success", "failure", "error"]
#: How many comment pages a lifecycle-comment lookup reads at most.
MAX_COMMENT_PAGES = 20
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_CHARS = 60_000
MAX_CODEOWNER_REVIEWERS = 10


@dataclass(frozen=True, slots=True)
class PullRequestComment:
    """One provider comment on a pull request.

    ``author_id`` is the provider's numeric user ID of the comment's author
    (``user.id`` on GitHub and Gitea, ``author.id`` on GitLab), ``None`` when
    the provider did not report one.
    """

    id: int
    body: str
    author_id: Optional[int] = None


class CommentUpsertResult(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class GitProviderError(RuntimeError):
    """Raised when a Git provider call fails with a user-actionable message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitProviderTransientError(GitProviderError):
    """A provider failure which may succeed when retried."""


class GitProviderRateLimitError(GitProviderTransientError):
    """The provider rate-limited the token; the request may be retried at ``retry_at``.

    Never an authorization failure: the provider must not be marked ``permission_denied``.
    """

    code = "provider-rate-limited"

    def __init__(self, message: str, *, status_code: int | None = None, retry_at: datetime | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_at = retry_at


def rate_limit_error(provider: str, response: httpx.Response, *, context: str) -> GitProviderRateLimitError | None:
    """A typed rate-limit error for ``response`` (counted once), or ``None`` when it is not one."""
    retry_at = rate_limit_retry_at(response)
    if retry_at is None:
        return None
    return GitProviderRateLimitError(
        f"{provider} rate limit reached on {context} (HTTP {response.status_code}); retry after {retry_at.isoformat()}",
        status_code=response.status_code,
        retry_at=retry_at,
    )


class GitProviderArchiveLimitError(GitProviderError):
    """A provider archive exceeded the caller's compressed-byte limit."""

    code = "repository-archive-limit-exceeded"


class GitConflictError(GitProviderError):
    """Raised when a commit cannot be applied to the expected repository revision."""

    code = "revision-conflict"


class ProviderCallBudgetExceededError(GitProviderError):
    """Raised before a request when the operation's provider call budget is spent."""

    code = "provider-call-budget-exceeded"


class GitProviderCapabilityError(GitProviderError):
    """Raised when an operation requires a capability the provider cannot guarantee."""

    code = "provider-capability-missing"

    def __init__(
        self,
        provider: str,
        capability: GitProviderCapability | str,
        *,
        reason: Optional[str] = None,
    ):
        self.provider = provider
        self.capability = capability.value if isinstance(capability, GitProviderCapability) else capability
        self.reason = reason
        detail = f" {reason}" if reason else ""
        super().__init__(f"Git provider '{provider}' does not support required capability '{self.capability}'.{detail}")


class PullRequestOptions(BaseModel):
    """Typed request options from the documented ``automation.pullRequests`` schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    draft: StrictBool = False
    labels: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()
    team_reviewers: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("teamReviewers", "team_reviewers"),
        serialization_alias="teamReviewers",
    )

    @field_validator("labels", "reviewers", "team_reviewers", mode="before")
    @classmethod
    def validate_name_lists(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("must be an array of non-empty strings")
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            if (
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in item)
            ):
                raise ValueError("must contain only non-empty strings without surrounding whitespace")
            if item in seen:
                raise ValueError("must not contain duplicate values")
            names.append(item)
            seen.add(item)
        return tuple(names)


EMPTY_PULL_REQUEST_OPTIONS = PullRequestOptions(labels=())
DEFAULT_AUTOMATION_PULL_REQUEST_OPTIONS = PullRequestOptions()


def normalize_git_provider_capability(capability: GitProviderCapability | str) -> GitProviderCapability | str:
    """Normalize known capability names while retaining forward-compatible strings."""
    if isinstance(capability, GitProviderCapability):
        return capability
    if not isinstance(capability, str):
        raise TypeError("Git provider capability names must be strings.")
    try:
        return GitProviderCapability(capability)
    except ValueError:
        return capability


def normalize_git_provider_capabilities(
    capabilities: Iterable[GitProviderCapability | str],
) -> frozenset[GitProviderCapability | str]:
    """Return a typed capability set from enum or legacy string declarations."""
    return frozenset(normalize_git_provider_capability(capability) for capability in capabilities)


def pull_request_option_capabilities(options: PullRequestOptions) -> frozenset[GitProviderCapability]:
    """Return the provider features required by non-empty PR options."""
    if not isinstance(options, PullRequestOptions):
        raise GitProviderError("Pull-request options must be a PullRequestOptions instance.")
    required: set[GitProviderCapability] = set()
    if options.draft:
        required.add(GitProviderCapability.DRAFT_PULL_REQUEST)
    if options.labels:
        required.add(GitProviderCapability.LABELS)
    if options.reviewers:
        required.add(GitProviderCapability.REVIEWERS)
    if options.team_reviewers:
        required.add(GitProviderCapability.TEAM_REVIEWERS)
    return frozenset(required)


# Characters and sequences that Git itself forbids in ref names. They are also
# the building blocks of URL/path injection, so both are rejected up front.
_GIT_REF_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\\]")
_GIT_REF_FORBIDDEN_SEQUENCES = ("..", "@{", "//")
_GREENKUBE_PLAN_BRANCH = re.compile(r"greenkube/plan/[0-9a-f]{32}\Z")


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


def validate_greenkube_plan_branch(ref: str) -> str:
    """Accept only the deterministic branch namespace created for automation plans."""
    value = validate_git_ref(ref, field="GreenKube plan branch")
    if value != ref or _GREENKUBE_PLAN_BRANCH.fullmatch(value) is None:
        raise GitProviderError("Refusing to operate on a branch outside the deterministic GreenKube plan namespace.")
    return value


def git_blob_id(content: str | bytes, *, object_format: Literal["sha1", "sha256"] = "sha1") -> str:
    """Return the Git blob object ID (``git hash-object``) of UTF-8 file content."""
    payload = content.encode("utf-8") if isinstance(content, str) else content
    header = b"blob " + str(len(payload)).encode("ascii") + b"\0"
    digest = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    return digest(header + payload).hexdigest()


def _validate_full_commit_sha(revision: object, *, provider: str, ref: str) -> str:
    if not isinstance(revision, str) or GIT_OBJECT_SHA.fullmatch(revision) is None:
        raise GitProviderError(f"{provider} returned an invalid full commit SHA for ref '{ref}'.")
    return revision


def _git_response_data(response: httpx.Response, *, provider: str, ref: str) -> object:
    try:
        return response.json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise GitProviderError(f"{provider} returned invalid JSON while resolving ref '{ref}'.") from exc


def _git_response_object(response: httpx.Response, *, provider: str, ref: str) -> dict:
    data = _git_response_data(response, provider=provider, ref=ref)
    if not isinstance(data, dict):
        raise GitProviderError(f"{provider} returned an invalid response while resolving ref '{ref}'.")
    return data


def quote_git_path(path: str) -> str:
    """Percent-encode a repository path for URL use, preserving ``/`` separators."""
    return quote(path.lstrip("/"), safe="/")


def _validate_git_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/") or "\\" in path:
        raise GitProviderError("Git file path must be a non-empty relative path.")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise GitProviderError("Git file path contains control characters.")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise GitProviderError("Git file path contains an invalid path component.")
    return path


@dataclass
class GitFile:
    """A versioned file returned by the provider."""

    path: str
    content: str
    sha: Optional[str] = None
    blob_sha: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    """One non-directory entry of a recursive repository tree."""

    path: str
    sha: str
    mode: str = "100644"
    type: str = "blob"


@dataclass(frozen=True, slots=True)
class GitTree:
    """A complete recursive listing of file entries at one revision."""

    entries: Mapping[str, GitTreeEntry]
    sha: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GitTreeChange:
    """One path whose entry differs between two trees.

    ``blob_sha`` is the head-side object ID when the provider reports it; it is
    ``None`` for deletions and for providers whose comparison API only lists
    paths (callers then read the head blob of the paths they care about).
    """

    path: str
    status: Literal["added", "modified", "deleted"]
    blob_sha: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GitTreeComparison:
    """The complete set of paths that differ between an immutable base and a head ref."""

    base_revision: str
    head_ref: str
    changes: tuple[GitTreeChange, ...] = ()
    head_revision: Optional[str] = None


def diff_git_trees(base: GitTree, head: GitTree) -> tuple[GitTreeChange, ...]:
    """Compare two complete trees by entry identity (object ID, mode and type)."""
    changes: list[GitTreeChange] = []
    for path in sorted(set(base.entries) | set(head.entries)):
        base_entry = base.entries.get(path)
        head_entry = head.entries.get(path)
        if head_entry is None:
            changes.append(GitTreeChange(path=path, status="deleted"))
        elif base_entry is None:
            changes.append(GitTreeChange(path=path, status="added", blob_sha=head_entry.sha))
        elif (base_entry.sha.lower(), base_entry.mode, base_entry.type) != (
            head_entry.sha.lower(),
            head_entry.mode,
            head_entry.type,
        ):
            changes.append(GitTreeChange(path=path, status="modified", blob_sha=head_entry.sha))
    return tuple(changes)


@dataclass
class GitFileEdit:
    """A create, update, or delete operation for one repository file."""

    path: str
    action: Literal["create", "update", "delete"]
    content: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_git_path(self.path)
        if self.action not in ("create", "update", "delete"):
            raise GitProviderError(f"Unsupported Git file action '{self.action}'.")
        if self.action == "delete":
            if self.content is not None:
                raise GitProviderError("Delete edits must not include file content.")
        elif not isinstance(self.content, str):
            raise GitProviderError(f"{self.action.capitalize()} edits require file content.")


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


@dataclass
class GitCommitRequest:
    """Atomic multi-file commit request for a provider branch."""

    repo: GitRepository
    branch: str
    base_revision: str
    message: str
    files: List[GitFileEdit]
    author_name: str
    author_email: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.repo, GitRepository):
            raise GitProviderError("Git commit request requires a GitRepository.")
        if not isinstance(self.branch, str) or validate_git_ref(self.branch, field="branch name") != self.branch:
            raise GitProviderError("Git commit branch must be a valid, unpadded ref name.")
        if (
            not isinstance(self.base_revision, str)
            or not self.base_revision
            or self.base_revision != self.base_revision.strip()
            or any(character.isspace() or ord(character) < 32 for character in self.base_revision)
        ):
            raise GitProviderError("Git commit request requires a valid base revision.")
        if not isinstance(self.message, str) or not self.message.strip() or "\x00" in self.message:
            raise GitProviderError("Git commit request requires a non-empty commit message.")
        if not isinstance(self.files, (list, tuple)) or not self.files:
            raise GitProviderError("Git commit request requires at least one file edit.")
        self.files = list(self.files)
        paths = []
        for edit in self.files:
            if not isinstance(edit, GitFileEdit):
                raise GitProviderError("Git commit request contains an invalid file edit.")
            edit.validate()
            paths.append(edit.path)
        if len(paths) != len(set(paths)):
            raise GitProviderError("Git commit request contains duplicate file paths.")
        for field_name, value in (("author name", self.author_name), ("author email", self.author_email)):
            if not isinstance(value, str) or not value.strip() or any(ord(character) < 32 for character in value):
                raise GitProviderError(f"Git commit request requires a valid {field_name}.")


#: Open pull requests read per repository and base branch by the plan trailer linkage.
OPEN_PULL_REQUEST_SCAN_LIMIT = 50
#: Commit messages read per pull request by the plan trailer linkage.
PULL_REQUEST_COMMIT_SCAN_LIMIT = 50


@dataclass
class GitCommitResult:
    """Result of a single atomic commit containing multiple file edits."""

    commit_sha: str
    branch: str
    files_changed: List[str]
    #: The platform's signature verification of the new commit (J8): True when it shows as
    #: Verified (GitHub App commits), False when unsigned or unverified, None when unknown.
    verified: Optional[bool] = None
    verification_reason: Optional[str] = None


def repository_matches_allowlist(repository_name: str, pattern: str) -> bool:
    """Match repository-name segments independently so globs cannot cross ``/``."""
    from fnmatch import fnmatchcase

    repository_segments = repository_name.split("/")
    pattern_segments = pattern.split("/")
    return len(repository_segments) == len(pattern_segments) and all(
        fnmatchcase(repository_segment, pattern_segment)
        for repository_segment, pattern_segment in zip(repository_segments, pattern_segments)
    )


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
    #: The platform signs the commits this adapter creates through its API (J8). A GitHub App
    #: installation commit shows as Verified only when it names no author or committer, so
    #: the adapter omits both and GitHub attributes the commit to the App's bot.
    platform_signed_commits: bool = False

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
        self._immutable_cache: OrderedDict[tuple[str, ...], object] = OrderedDict()

    capabilities: frozenset[GitProviderCapability] = frozenset()

    def supports(self, capability: GitProviderCapability | str) -> bool:
        """Returns whether this provider can guarantee the named capability."""
        return normalize_git_provider_capability(capability) in normalize_git_provider_capabilities(self.capabilities)

    def validate_pull_request_options(self, options: PullRequestOptions | None = None) -> PullRequestOptions:
        """Fail before mutation when this adapter cannot honor a requested PR option."""
        selected = options if options is not None else EMPTY_PULL_REQUEST_OPTIONS
        if not isinstance(selected, PullRequestOptions):
            raise GitProviderError("create_pull_request requires PullRequestOptions.")
        required = pull_request_option_capabilities(selected)
        available = normalize_git_provider_capabilities(self.capabilities)
        missing = required - available
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise GitProviderCapabilityError(self.name, names)
        self._validate_provider_pull_request_options(selected)
        return selected

    def _validate_provider_pull_request_options(self, options: PullRequestOptions) -> None:
        """Validate option values whose request format is provider-specific."""

    async def ensure_pull_request_options(
        self,
        repo: GitRepository,
        pull_request: "PullRequestInfo",
        options: PullRequestOptions | None = None,
    ) -> None:
        """Ensure PR metadata options are applied when a matching request is reused."""
        self.validate_pull_request_options(options)
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request option update targets provider '{repo.provider}', not '{self.name}'.")
        if (
            isinstance(pull_request.number, bool)
            or not isinstance(pull_request.number, int)
            or pull_request.number <= 0
        ):
            raise GitProviderError("Pull-request number must be a positive integer.")

    def _validate_commit_request(self, request: GitCommitRequest) -> None:
        if not isinstance(request, GitCommitRequest):
            raise GitProviderError("create_commit requires a GitCommitRequest.")
        request.validate()
        if request.repo.provider != self.name:
            raise GitProviderError(f"Git commit request targets provider '{request.repo.provider}', not '{self.name}'.")
        if not self.supports("multi_file_commit"):
            raise GitProviderCapabilityError(self.name, "multi_file_commit")

    def _validate_ref_request(self, repo: GitRepository, ref: str) -> str:
        if not isinstance(repo, GitRepository):
            raise GitProviderError("Ref lookup requires a GitRepository.")
        if repo.provider != self.name:
            raise GitProviderError(f"Ref lookup targets provider '{repo.provider}', not '{self.name}'.")
        if not isinstance(ref, str):
            raise GitProviderError("Invalid git ref: expected a string.")
        validated_ref = validate_git_ref(ref, field="git ref")
        if validated_ref != ref:
            raise GitProviderError("Invalid git ref: leading or trailing whitespace is not allowed.")
        return validated_ref

    def _validate_archive_request(self, repo: GitRepository, revision: str, max_bytes: int) -> str:
        if not isinstance(repo, GitRepository):
            raise GitProviderError("Repository archive download requires a GitRepository.")
        if repo.provider != self.name:
            raise GitProviderError(
                f"Repository archive download targets provider '{repo.provider}', not '{self.name}'."
            )
        if type(max_bytes) is not int or max_bytes <= 0:
            raise GitProviderError("Repository archive download requires a positive byte limit.")
        return _validate_full_commit_sha(revision, provider=self.name, ref=str(revision))

    async def _validate_commit_edits(self, request: GitCommitRequest) -> dict[str, Optional[GitFile]]:
        """Check file-operation preconditions against the requested base revision."""
        files: dict[str, Optional[GitFile]] = {}
        for edit in request.files:
            current = await self._get_file_cached(request.repo, edit.path, request.base_revision)
            if edit.action == "create" and current is not None:
                raise GitConflictError(f"Cannot create '{edit.path}': the file already exists at the base revision.")
            if edit.action != "create" and current is None:
                raise GitConflictError(f"Cannot {edit.action} '{edit.path}': the file is missing at the base revision.")
            files[edit.path] = current
        return files

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
    # Immutable-revision cache
    # ------------------------------------------------------------------

    def _cache_get(self, key: tuple[str, ...]) -> object | None:
        cache = getattr(self, "_immutable_cache", None)
        if cache is None or key not in cache:
            return None
        cache.move_to_end(key)
        return cache[key]

    def _cache_put(self, key: tuple[str, ...], value: object) -> None:
        cache = getattr(self, "_immutable_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._immutable_cache = cache
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _IMMUTABLE_CACHE_SIZE:
            cache.popitem(last=False)

    async def _get_file_cached(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]:
        """Read a file once per immutable commit SHA; mutable refs are always re-read."""
        if not is_full_commit_sha(ref):
            return await self.get_file(repo, path, ref)
        key = ("file", repo.full_name, ref.lower(), path)
        cached = self._cache_get(key)
        if isinstance(cached, _CachedFile):
            return cached.file
        git_file = await self.get_file(repo, path, ref)
        self._cache_put(key, _CachedFile(git_file))
        return git_file

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
        conflict_statuses: tuple[int, ...] = (),
        retryable: bool = True,
    ) -> httpx.Response:
        client = await self._client_or_create()
        url = f"{self.api_base_url}{path}" if self.api_base_url else path
        for attempt in range(3 if retryable else 1):
            charge_provider_call(self.name)
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
                if retryable and attempt < 2:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise GitProviderTransientError(f"{self.name} transient request failure: {exc}") from exc
            except httpx.HTTPError as exc:
                raise GitProviderError(f"{self.name} request failed: {exc}") from exc
            if response.status_code in (403, 429):
                rate_limited = rate_limit_error(self.name, response, context=f"{method} {path}")
                if rate_limited is not None:
                    record_rate_limited(self.name)
                    wait = _inline_retry_wait(response, attempt)
                    if retryable and attempt < 2 and wait is not None:
                        await asyncio.sleep(wait)
                        continue
                    raise rate_limited
            if retryable and response.status_code in (500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(_inline_retry_wait(response, attempt) or 0.05 * (2**attempt))
                continue
            break

        if response.status_code in conflict_statuses:
            detail = _error_detail(response)
            raise GitConflictError(
                f"{self.name} revision conflict on {method} {path}: {detail}",
                status_code=response.status_code,
            )
        if response.status_code == 404:
            return response
        if response.status_code not in ok_statuses:
            detail = _error_detail(response)
            if response.status_code in (429, 500, 502, 503, 504):
                raise GitProviderTransientError(
                    f"{self.name} transient API error {response.status_code} on {method} {path}: {detail}",
                    status_code=response.status_code,
                )
            raise GitProviderError(
                f"{self.name} API error {response.status_code} on {method} {path}: {detail}",
                status_code=response.status_code,
            )
        return response

    async def _request_archive(
        self,
        path: str,
        *,
        max_bytes: int,
        params: Optional[dict] = None,
        redirect_hosts: frozenset[str] = frozenset(),
    ) -> bytes:
        """Stream an archive response without exceeding the caller's compressed-byte limit."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise GitProviderError("Repository archive download requires a positive byte limit.")
        client = await self._client_or_create()
        initial_url = f"{self.api_base_url}{path}" if self.api_base_url else path
        retryable_statuses = (429, 500, 502, 503, 504)
        redirect_statuses = (301, 302, 303, 307, 308)

        for attempt in range(3):
            current_url = initial_url
            current_params = params
            current_headers = self._headers()
            retry_response = False
            try:
                redirects = 0
                while True:
                    charge_provider_call(self.name)
                    async with client.stream(
                        "GET",
                        current_url,
                        params=current_params,
                        headers=current_headers,
                        auth=None,
                        timeout=self.timeout,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (403, 429):
                            await response.aread()
                            rate_limited = rate_limit_error(self.name, response, context="archive download")
                            if rate_limited is not None:
                                record_rate_limited(self.name)
                                if attempt >= 2 or _inline_retry_wait(response, attempt) is None:
                                    raise rate_limited
                        if response.status_code in retryable_statuses:
                            if attempt < 2:
                                retry_response = True
                                break
                            raise GitProviderTransientError(
                                f"{self.name} archive request failed with HTTP {response.status_code}.",
                                status_code=response.status_code,
                            )

                        if response.status_code in redirect_statuses:
                            if redirects >= 3:
                                raise GitProviderError("Provider archive redirect limit was exceeded.")
                            location = response.headers.get("location")
                            if not location:
                                raise GitProviderError("Provider returned an invalid archive redirect.")
                            current_url, same_origin = _archive_redirect_target(
                                str(response.url),
                                location,
                                allowed_hosts=redirect_hosts,
                            )
                            if not same_origin:
                                current_headers = {}
                            current_params = None
                            redirects += 1
                            continue

                        if response.status_code == 404:
                            raise GitProviderError(
                                "Repository archive was not found at the requested immutable commit.",
                                status_code=response.status_code,
                            )
                        if response.status_code != 200:
                            raise GitProviderError(
                                f"{self.name} archive request failed with HTTP {response.status_code}.",
                                status_code=response.status_code,
                            )

                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                declared_size = int(content_length)
                            except ValueError:
                                raise GitProviderError(
                                    "Provider returned an invalid repository archive size."
                                ) from None
                            if declared_size < 0:
                                raise GitProviderError("Provider returned an invalid repository archive size.")
                            if declared_size > max_bytes:
                                raise GitProviderArchiveLimitError(
                                    "Provider repository archive exceeds the configured compressed-byte limit."
                                )

                        content = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            if len(chunk) > max_bytes - len(content):
                                raise GitProviderArchiveLimitError(
                                    "Provider repository archive exceeds the configured compressed-byte limit."
                                )
                            content.extend(chunk)
                        return bytes(content)

                if retry_response:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt < 2:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise GitProviderTransientError(f"{self.name} transient archive request failure.") from None
            except httpx.HTTPError:
                raise GitProviderError(f"{self.name} archive request failed.") from None

        raise GitProviderTransientError(f"{self.name} transient archive request failure.")

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

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
        """Returns the full commit SHA for a branch or tag."""
        self._validate_ref_request(repo, ref)
        raise GitProviderCapabilityError(
            self.name,
            "get_ref_revision",
            reason="This adapter does not implement provider-native ref resolution.",
        )

    @abstractmethod
    async def get_default_branch(self, repo: GitRepository) -> str: ...

    @abstractmethod
    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]: ...

    @abstractmethod
    async def list_files(self, repo: GitRepository, ref: str) -> List[str]: ...

    async def get_tree(self, repo: GitRepository, ref: str) -> GitTree:
        """Return every file entry at ``ref`` with its object ID.

        This portable fallback costs one call per file; adapters override it
        with their recursive tree API so cost does not grow with repository size.
        """
        entries: dict[str, GitTreeEntry] = {}
        for path in await self.list_files(repo, ref):
            git_file = await self._get_file_cached(repo, path, ref)
            if git_file is None:
                raise GitProviderError(f"{self.name} could not read listed file {path!r} at {ref!r}.")
            entries[path] = GitTreeEntry(path=path, sha=git_file.blob_sha or git_blob_id(git_file.content))
        return GitTree(entries=entries)

    async def _get_tree_cached(self, repo: GitRepository, ref: str) -> GitTree:
        if not is_full_commit_sha(ref):
            return await self.get_tree(repo, ref)
        key = ("tree", repo.full_name, ref.lower())
        cached = self._cache_get(key)
        if isinstance(cached, GitTree):
            return cached
        tree = await self.get_tree(repo, ref)
        self._cache_put(key, tree)
        return tree

    async def compare_trees(self, repo: GitRepository, base_revision: str, head_ref: str) -> GitTreeComparison:
        """Return every path whose entry differs between ``base_revision`` and ``head_ref``.

        ``base_revision`` must be an immutable full commit SHA. The result is a
        direct (two-dot) tree comparison and is always complete: adapters whose
        fast comparison API cannot prove completeness fall back to full trees.
        """
        base_revision = _validate_full_commit_sha(base_revision, provider=self.name, ref=base_revision)
        base_tree = await self._get_tree_cached(repo, base_revision)
        head_tree = await self._get_tree_cached(repo, head_ref)
        return GitTreeComparison(
            base_revision=base_revision,
            head_ref=head_ref,
            changes=diff_git_trees(base_tree, head_tree),
            head_revision=head_ref if is_full_commit_sha(head_ref) else None,
        )

    async def get_blob_ids(self, repo: GitRepository, ref: str, paths: Sequence[str]) -> dict[str, Optional[str]]:
        """Return the blob object ID of each path at ``ref`` (``None`` when absent).

        Costs at most one call per requested path; results for immutable commit
        SHAs are cached for the lifetime of the adapter.
        """
        blob_ids: dict[str, Optional[str]] = {}
        for path in paths:
            _validate_git_path(path)
            git_file = await self._get_file_cached(repo, path, ref)
            blob_ids[path] = None if git_file is None else git_file.blob_sha or git_blob_id(git_file.content)
        return blob_ids

    async def is_ancestor(self, repo: GitRepository, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
        """Return whether ``descendant_sha`` contains ``ancestor_sha`` in its history.

        ``True`` also covers identical commits. ``None`` means the provider
        cannot tell (unknown commit, unsupported API); callers must not treat
        it as a negative answer.
        """
        self._validate_ancestry_request(repo, ancestor_sha, descendant_sha)
        raise GitProviderCapabilityError(
            self.name,
            GitProviderCapability.COMMIT_ANCESTRY,
            reason="This adapter does not implement commit ancestry checks.",
        )

    def _validate_ancestry_request(
        self,
        repo: GitRepository,
        ancestor_sha: str,
        descendant_sha: str,
    ) -> tuple[str, str]:
        if not isinstance(repo, GitRepository) or repo.provider != self.name:
            raise GitProviderError(f"Ancestry check must target a {self.name} repository.")
        ancestor = _validate_full_commit_sha(ancestor_sha, provider=self.name, ref=ancestor_sha)
        descendant = _validate_full_commit_sha(descendant_sha, provider=self.name, ref=descendant_sha)
        return ancestor.lower(), descendant.lower()

    async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
        """Return a bounded archive for one immutable full commit SHA."""
        self._validate_archive_request(repo, revision, max_bytes)
        raise GitProviderCapabilityError(
            self.name,
            GitProviderCapability.REPOSITORY_ARCHIVE,
            reason="This adapter does not provide bounded archives for immutable commits.",
        )

    @abstractmethod
    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None: ...

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        """Creates one native atomic commit, or fails when the provider cannot guarantee it."""
        self._validate_commit_request(request)
        raise GitProviderCapabilityError(
            self.name,
            "multi_file_commit",
            reason="This adapter does not implement a native atomic commit operation.",
        )

    @abstractmethod
    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> "PullRequestInfo": ...

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional["PullRequestInfo"]:
        """Finds an existing open request for an exact branch pair when supported."""
        raise GitProviderCapabilityError(
            self.name,
            "find_pull_request",
            reason="This adapter does not implement provider-side pull-request lookup.",
        )

    async def find_terminal_pull_request(
        self, repo: GitRepository, head: str, base: str
    ) -> Optional["PullRequestInfo"]:
        """Finds the most recently updated merged or closed request for an exact branch pair (D10).

        Adapters without the lookup return ``None``: the caller then behaves as before.
        """
        return None

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> Optional["PullRequestInfo"]:
        """Finds any open request using a deterministic plan branch as head or base."""
        self._validate_plan_branch_request(repo, branch)
        raise GitProviderCapabilityError(
            self.name,
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            reason="This adapter cannot prove that a plan branch has no open pull requests.",
        )

    async def delete_plan_branch(self, repo: GitRepository, branch: str) -> None:
        """Deletes an owned deterministic plan branch; missing branches must be treated as success."""
        self._validate_plan_branch_request(repo, branch)
        raise GitProviderCapabilityError(
            self.name,
            GitProviderCapability.DELETE_PLAN_BRANCH,
            reason="This adapter cannot safely delete a deterministic plan branch.",
        )

    @abstractmethod
    async def get_pull_request(self, repo: GitRepository, number: int) -> "PullRequestInfo": ...

    def _validate_plan_branch_request(self, repo: GitRepository, branch: str) -> str:
        if not isinstance(repo, GitRepository):
            raise GitProviderError("Plan branch operations require a GitRepository.")
        if repo.provider != self.name:
            raise GitProviderError(f"Plan branch operation targets provider '{repo.provider}', not '{self.name}'.")
        return validate_greenkube_plan_branch(branch)

    # ------------------------------------------------------------------
    # Pull-request experience (roadmap J2, J3, J5)
    # ------------------------------------------------------------------

    def _validate_pull_request_number(self, repo: GitRepository, number: object) -> int:
        if not isinstance(repo, GitRepository) or repo.provider != self.name:
            raise GitProviderError(f"Pull-request operation must target a {self.name} repository.")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")
        return number

    async def add_pull_request_metadata(
        self,
        repo: GitRepository,
        pull_request: "PullRequestInfo",
        *,
        labels: Sequence[str] = (),
        reviewers: Sequence[str] = (),
        team_reviewers: Sequence[str] = (),
    ) -> None:
        """Add labels and reviewers to an open request without removing existing ones.

        Missing labels are created where the provider allows it (GitHub and
        GitLab create them implicitly; Gitea through its label API).
        """
        raise GitProviderCapabilityError(self.name, GitProviderCapability.PULL_REQUEST_METADATA)

    async def set_commit_status(
        self,
        repo: GitRepository,
        sha: str,
        *,
        state: CommitStatusState,
        context: str,
        description: str,
        target_url: Optional[str] = None,
    ) -> None:
        """Publish a commit status (e.g. ``greenkube/validation``) on ``sha``."""
        raise GitProviderCapabilityError(self.name, GitProviderCapability.COMMIT_STATUS)

    def _validate_commit_status(self, repo: GitRepository, sha: str, state: str, context: str) -> str:
        if not isinstance(repo, GitRepository) or repo.provider != self.name:
            raise GitProviderError(f"Commit status must target a {self.name} repository.")
        if state not in ("pending", "success", "failure", "error"):
            raise GitProviderError(f"Unsupported commit status state {state!r}.")
        if not isinstance(context, str) or not context.strip() or len(context) > 255:
            raise GitProviderError("Commit status context must be 1-255 characters.")
        return _validate_full_commit_sha(sha, provider=self.name, ref=str(sha))

    # -- plan trailer linkage (F4 patch mode) -------------------------------------------

    def _open_pull_requests_query(self, repo: GitRepository, base: str, limit: int) -> tuple[str, dict[str, str]]:
        raise GitProviderCapabilityError(self.name, GitProviderCapability.FIND_PULL_REQUEST)

    def _pull_request_commits_path(self, repo: GitRepository, number: int) -> str:
        raise GitProviderCapabilityError(self.name, GitProviderCapability.FIND_PULL_REQUEST)

    async def list_open_pull_requests(
        self, repo: GitRepository, base: str, *, limit: int = OPEN_PULL_REQUEST_SCAN_LIMIT
    ) -> List[PullRequestInfo]:
        """The most recent open pull requests targeting ``base`` (one page, at most ``limit``).

        Used to link a ``patch``-mode pull request opened from any branch to its plan by
        the ``GreenKube-Plan`` trailer. Items that do not validate are skipped.
        """
        base = validate_git_ref(base, field="base branch")
        limit = max(1, min(int(limit), 100))
        path, params = self._open_pull_requests_query(repo, base, limit)
        response = await self._request("GET", path, params=params)
        if response.status_code == 404:
            raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
        result: List[PullRequestInfo] = []
        for item in parse_pull_request_list(response, repo=repo)[:limit]:
            base_data = item.get("base")
            target = base_data.get("ref") if isinstance(base_data, dict) else item.get("target_branch")
            if target != base:
                continue
            try:
                result.append(normalize_pull_request(item, repo=repo))
            except GitProviderError:
                continue
        return result

    async def list_pull_request_commit_messages(
        self, repo: GitRepository, number: int, *, limit: int = PULL_REQUEST_COMMIT_SCAN_LIMIT
    ) -> List[str]:
        """Commit messages of a pull request (first page, at most ``limit``)."""
        number = self._validate_pull_request_number(repo, number)
        limit = max(1, min(int(limit), 100))
        response = await self._request(
            "GET",
            self._pull_request_commits_path(repo, number),
            params={"per_page": str(limit), "limit": str(limit)},
        )
        if response.status_code == 404:
            raise GitProviderError(f"Could not list the commits of pull request {number} in {repo.full_name}.")
        try:
            data = response.json()
        except ValueError:
            raise GitProviderError(f"{self.name} returned an invalid pull-request commit list.") from None
        if not isinstance(data, list):
            raise GitProviderError(f"{self.name} returned an invalid pull-request commit list.")
        messages: List[str] = []
        for item in data[:limit]:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit")
            message = commit.get("message") if isinstance(commit, dict) else item.get("message")
            if isinstance(message, str):
                messages.append(message)
        return messages

    async def upsert_pull_request_comment(
        self,
        repo: GitRepository,
        number: int,
        *,
        marker: str,
        body: str,
    ) -> tuple[CommentUpsertResult, int]:
        """Create or edit the one comment whose body starts with ``marker``; never post a duplicate.

        An identical body is not written again. Returns the outcome and the comment ID.
        """
        if not self.supports(GitProviderCapability.PULL_REQUEST_COMMENTS):
            raise GitProviderCapabilityError(self.name, GitProviderCapability.PULL_REQUEST_COMMENTS)
        number = self._validate_pull_request_number(repo, number)
        if not isinstance(marker, str) or not marker.strip() or not body.startswith(marker):
            raise GitProviderError("A managed pull-request comment must start with its marker.")
        if len(body) > MAX_COMMENT_CHARS or "\x00" in body:
            raise GitProviderError("Pull-request comment is too long or contains a NUL character.")
        author_id = await self._managed_comment_author_id(repo, number)
        existing = await self._find_marked_comment(repo, number, marker, author_id=author_id)
        if existing is None:
            created = await self._create_pull_request_comment(repo, number, body)
            return CommentUpsertResult.CREATED, created
        if existing.body.strip() == body.strip():
            return CommentUpsertResult.UNCHANGED, existing.id
        await self._update_pull_request_comment(repo, number, existing.id, body)
        return CommentUpsertResult.UPDATED, existing.id

    async def _managed_comment_author_id(self, repo: GitRepository, number: int) -> int:
        """Return the user ID whose marker comments GreenKube may adopt and edit.

        A marker is plain text that anyone with comment access can post, and
        GreenKube's token can edit other users' comments. Adopting a foreign
        marker comment would publish GreenKube's status under (and editable by)
        someone else, or overwrite a human's text. Only comments written by
        GreenKube's own identity are adopted: the token's user (``GET /user``)
        or, when the token cannot read its own profile, the author of the pull
        request, which GreenKube opened.
        """
        user_id = await self._authenticated_user_id()
        if user_id is None:
            pull_request = await self.get_pull_request(repo, number)
            user_id = pull_request_author_id(pull_request.raw)
        if user_id is None:
            raise GitProviderError(
                f"{self.name} did not report GreenKube's identity; the lifecycle comment is not published."
            )
        return user_id

    async def _authenticated_user_id(self) -> Optional[int]:
        """The numeric ID of the token's user, or ``None`` when the token may not read it."""
        response = await self._request("GET", "/user", ok_statuses=(200, 401, 403))
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return _positive_user_id(data.get("id") if isinstance(data, dict) else None)

    async def _find_marked_comment(
        self, repo: GitRepository, number: int, marker: str, *, author_id: int
    ) -> Optional[PullRequestComment]:
        for page in range(1, MAX_COMMENT_PAGES + 1):
            comments, has_next = await self._list_pull_request_comments(repo, number, page)
            for comment in comments:
                if comment.author_id == author_id and comment.body.lstrip().startswith(marker):
                    return comment
            if not has_next:
                return None
        raise GitProviderError("Pull-request comment pagination exceeded the safety limit.")

    async def _list_pull_request_comments(
        self, repo: GitRepository, number: int, page: int
    ) -> tuple[list[PullRequestComment], bool]:
        raise GitProviderCapabilityError(self.name, GitProviderCapability.PULL_REQUEST_COMMENTS)

    async def _create_pull_request_comment(self, repo: GitRepository, number: int, body: str) -> int:
        raise GitProviderCapabilityError(self.name, GitProviderCapability.PULL_REQUEST_COMMENTS)

    async def _update_pull_request_comment(self, repo: GitRepository, number: int, comment_id: int, body: str) -> None:
        raise GitProviderCapabilityError(self.name, GitProviderCapability.PULL_REQUEST_COMMENTS)

    async def read_codeowners(self, repo: GitRepository, revision: str):
        """Parse the CODEOWNERS file at ``revision`` from the provider's documented locations.

        Returns a :class:`~greenkube.automation.pr_content.codeowners.CodeOwners`
        or ``None`` when the repository has none. Blob IDs come from the cached
        tree where the adapter has one, so a missing file usually costs no call.
        """
        from greenkube.automation.pr_content.codeowners import CODEOWNERS_PATHS, Dialect, parse_codeowners

        dialect: Dialect = "gitlab" if self.name == "gitlab" else "gitea" if self.name == "gitea" else "github"
        paths = CODEOWNERS_PATHS[dialect]
        blob_ids = await self.get_blob_ids(repo, revision, paths)
        for path in paths:
            if not blob_ids.get(path):
                continue
            git_file = await self._get_file_cached(repo, path, revision)
            if git_file is not None:
                return parse_codeowners(git_file.content, dialect)
        return None

    async def codeowner_reviewers(
        self,
        repo: GitRepository,
        owners: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """Map CODEOWNERS owners to ``(reviewers, team_reviewers)`` this provider accepts.

        ``@user`` becomes a user reviewer and ``@org/team`` a team of the
        repository's organization; e-mail owners are skipped.
        """
        users: list[str] = []
        teams: list[str] = []
        for owner in owners:
            if not owner.startswith("@"):
                continue
            name = owner[1:]
            if "/" in name:
                organization, _, team = name.partition("/")
                if organization.casefold() == repo.owner.casefold() and team and "/" not in team:
                    if team not in teams:
                        teams.append(team)
            elif name and name not in users:
                users.append(name)
        return users[:MAX_CODEOWNER_REVIEWERS], teams[:MAX_CODEOWNER_REVIEWERS]

    async def test_connection(self) -> bool:
        """Checks that the provider is reachable and the token is valid."""
        try:
            await self._request("GET", "/user")
            return True
        except GitProviderError:
            return False


@dataclass(frozen=True, slots=True)
class _CachedFile:
    file: Optional[GitFile]


def _inline_retry_wait(response: httpx.Response, attempt: int) -> float | None:
    """Seconds to sleep before retrying ``response`` within the same request, or ``None``.

    A provider-announced wait longer than :data:`MAX_INLINE_RATE_LIMIT_WAIT_SECONDS` is not
    slept through: the caller gets a typed error and reschedules at the reset time.
    """
    backoff = 0.05 * (2**attempt)
    announced = announced_wait_seconds(response.headers, now=datetime.now(timezone.utc))
    if announced is None:
        return backoff
    if announced > MAX_INLINE_RATE_LIMIT_WAIT_SECONDS:
        return None
    return max(announced, backoff)


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


def _archive_redirect_target(
    current_url: str,
    location: str,
    *,
    allowed_hosts: frozenset[str],
) -> tuple[str, bool]:
    """Resolve a provider archive redirect while keeping credentials on the API origin."""
    try:
        target_url = urljoin(current_url, location)
        current = urlsplit(current_url)
        target = urlsplit(target_url)
        current_host = current.hostname
        target_host = target.hostname
        current_port = current.port
        target_port = target.port
        current_effective_port = (
            current_port if current_port is not None else (443 if current.scheme == "https" else 80)
        )
        target_effective_port = target_port if target_port is not None else (443 if target.scheme == "https" else 80)
    except ValueError:
        raise GitProviderError("Provider returned an invalid archive redirect.") from None

    if (
        current_host is None
        or target_host is None
        or target.username is not None
        or target.password is not None
        or target.fragment
        or target.scheme not in {"http", "https"}
    ):
        raise GitProviderError("Provider returned an invalid archive redirect.")

    same_origin = (
        current.scheme == target.scheme
        and current_host.lower() == target_host.lower()
        and current_effective_port == target_effective_port
    )
    if not same_origin and (
        target.scheme != "https"
        or target_effective_port != 443
        or target_host.lower() not in {host.lower() for host in allowed_hosts}
    ):
        raise GitProviderError("Provider returned an unsupported archive redirect.")
    return target_url, same_origin


class MergeCommitSource(StrEnum):
    """Which provider field identified the commit that landed on the base branch."""

    MERGE_COMMIT = "merge_commit"
    SQUASH_COMMIT = "squash_commit"
    FAST_FORWARD_HEAD = "fast_forward_head"


@dataclass
class PullRequestInfo:
    """Normalized pull-request information across providers.

    ``merge_commit_sha`` is set only for merged requests and names the commit
    that landed on the base branch; ``merge_commit_source`` records which
    provider field it came from (:class:`MergeCommitSource`).
    """

    number: int
    url: str
    status: str
    merged: bool = False
    raw: dict = field(default_factory=dict)
    head_branch: Optional[str] = None
    base_branch: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    merge_commit_source: Optional[str] = None

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
    head = data.get("head")
    base = data.get("base")
    head_branch = head.get("ref") if isinstance(head, dict) else data.get("source_branch")
    base_branch = base.get("ref") if isinstance(base, dict) else data.get("target_branch")
    # GitHub keeps a test-merge ``merge_commit_sha`` on requests closed without
    # merging; only a merged request has a merge commit (BUG-45).
    raw_merge_sha = data.get("merge_commit_sha")
    merge_commit_sha = raw_merge_sha if merged and is_full_commit_sha(raw_merge_sha) else None
    return PullRequestInfo(
        number=number,
        url=url,
        status=status,
        merged=merged,
        raw=data,
        head_branch=head_branch if isinstance(head_branch, str) else None,
        base_branch=base_branch if isinstance(base_branch, str) else None,
        merge_commit_sha=merge_commit_sha,
        merge_commit_source=MergeCommitSource.MERGE_COMMIT.value if merge_commit_sha is not None else None,
    )


def parse_comment_list(response: httpx.Response, *, provider: str) -> list[PullRequestComment]:
    """Validate a provider's comment (or note) list: objects with an integer ``id`` and a ``body``."""
    try:
        data = response.json()
    except ValueError as exc:
        raise GitProviderError(f"{provider} returned invalid JSON for a comment list.") from exc
    if not isinstance(data, list):
        raise GitProviderError(f"{provider} returned an invalid comment list.")
    comments: list[PullRequestComment] = []
    for item in data:
        comment_id = item.get("id") if isinstance(item, dict) else None
        body = item.get("body") if isinstance(item, dict) else None
        if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
            raise GitProviderError(f"{provider} returned a comment without a valid ID.")
        author = item.get("user") or item.get("author")
        comments.append(
            PullRequestComment(
                id=comment_id,
                body=body if isinstance(body, str) else "",
                author_id=_positive_user_id(author.get("id") if isinstance(author, dict) else None),
            )
        )
    return comments


def pull_request_author_id(raw: object) -> Optional[int]:
    """The author's user ID from a raw PR payload (``user`` on GitHub/Gitea, ``author`` on GitLab)."""
    if not isinstance(raw, dict):
        return None
    author = raw.get("user") or raw.get("author")
    return _positive_user_id(author.get("id") if isinstance(author, dict) else None)


def _positive_user_id(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def parse_comment_id(response: httpx.Response, *, provider: str) -> int:
    try:
        data = response.json()
    except ValueError as exc:
        raise GitProviderError(f"{provider} returned invalid JSON after creating a comment.") from exc
    comment_id = data.get("id") if isinstance(data, dict) else None
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
        raise GitProviderError(f"{provider} returned an invalid comment after creating it.")
    return comment_id


def parse_pull_request_list(response: httpx.Response, *, repo: GitRepository) -> list[dict]:
    """Validates a provider's list response before a lookup can be treated as complete."""
    try:
        data = response.json()
    except ValueError as exc:
        raise GitProviderError(f"{repo.provider} returned invalid JSON for its pull-request list.") from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request list.")
    return data


def pull_request_next_page(response: httpx.Response, *, repo: GitRepository, current_page: int) -> Optional[int]:
    """Returns a validated next-page cursor from a provider Link header."""
    next_link = response.links.get("next")
    if next_link is None:
        return None
    next_url = next_link.get("url") if isinstance(next_link, dict) else None
    query = parse_qs(urlsplit(next_url).query) if isinstance(next_url, str) else {}
    page_values = query.get("page")
    if not page_values or len(page_values) != 1:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request pagination link.")
    try:
        page = int(page_values[0])
    except ValueError as exc:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request pagination cursor.") from exc
    if page <= current_page:
        raise GitProviderError(f"{repo.provider} pull-request pagination did not progress.")
    return page


def normalize_open_pull_request_match(
    data: dict,
    *,
    repo: GitRepository,
    head: str,
    base: str,
    provider_head: object,
    provider_base: object,
    open_states: tuple[str, ...],
) -> Optional[PullRequestInfo]:
    """Normalizes a list item and returns it only when its open branch pair matches exactly."""
    if not isinstance(provider_head, str) or not provider_head:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request head branch.")
    if not isinstance(provider_base, str) or not provider_base:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request base branch.")
    state = data.get("state")
    if not isinstance(state, str) or not state:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request state.")
    pull_request = normalize_pull_request(data, repo=repo)
    if state not in open_states or provider_head != head or provider_base != base or pull_request.merged:
        return None
    return pull_request


def normalize_terminal_pull_request_match(
    data: dict,
    *,
    repo: GitRepository,
    head: str,
    base: str,
    provider_head: object,
    provider_base: object,
    open_states: tuple[str, ...],
) -> Optional[PullRequestInfo]:
    """Normalizes a merged or closed request whose branch pair matches exactly (D10).

    Open requests are left to :func:`normalize_open_pull_request_match`.
    """
    if not isinstance(provider_head, str) or not provider_head:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request head branch.")
    if not isinstance(provider_base, str) or not provider_base:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request base branch.")
    state = data.get("state")
    if not isinstance(state, str) or not state:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request state.")
    pull_request = normalize_pull_request(data, repo=repo)
    if provider_head != head or provider_base != base or (state in open_states and not pull_request.merged):
        return None
    return pull_request


def normalize_open_pull_request_branch_match(
    data: dict,
    *,
    repo: GitRepository,
    branch: str,
    provider_head: object,
    provider_base: object,
    open_states: tuple[str, ...],
) -> Optional[PullRequestInfo]:
    """Normalizes an open request only when the branch is its exact head or base."""
    if not isinstance(provider_head, str) or not provider_head:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request head branch.")
    if not isinstance(provider_base, str) or not provider_base:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request base branch.")
    state = data.get("state")
    if not isinstance(state, str) or not state:
        raise GitProviderError(f"{repo.provider} returned an invalid pull-request state.")
    pull_request = normalize_pull_request(data, repo=repo)
    if state not in open_states or branch not in {provider_head, provider_base} or pull_request.merged:
        return None
    return pull_request
