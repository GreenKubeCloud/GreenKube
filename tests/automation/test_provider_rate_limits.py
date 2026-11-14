"""Git provider rate limits are transient, rescheduled at the provider's reset time (provider resilience)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import (
    GitProviderError,
    GitProviderRateLimitError,
    GitProviderTransientError,
)
from greenkube.automation.git.gitea import GiteaProvider
from greenkube.automation.git.github import GitHubProvider
from greenkube.automation.git.gitlab import GitLabProvider
from greenkube.automation.git.rate_limit import (
    GIT_PROVIDER_RATE_LIMITED,
    rate_limit_retry_at_of,
)
from greenkube.automation.git.registry import ProviderCheckRecorder, _redact_provider_error

REPO_URL = "https://api.github.com/repos/acme/manifests"


def _rate_limited_count(provider: str) -> float:
    return GIT_PROVIDER_RATE_LIMITED.labels(provider=provider)._value.get()


def _close_to(actual: datetime | None, expected: datetime, *, tolerance: float = 3.0) -> bool:
    return actual is not None and abs((actual - expected).total_seconds()) <= tolerance


@pytest.mark.asyncio
@respx.mock
async def test_github_primary_limit_403_is_rate_limited_with_reset_time():
    reset = datetime.now(timezone.utc) + timedelta(minutes=20)
    route = respx.get(REPO_URL).mock(
        return_value=Response(
            403,
            json={"message": "API rate limit exceeded for installation ID 1."},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(reset.timestamp()))},
        )
    )
    before = _rate_limited_count("github")
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider.get_default_branch(repo)

    assert isinstance(raised.value, GitProviderTransientError)
    assert raised.value.status_code == 403
    # Never slept through a 20-minute reset inside the request: one call, then reschedule.
    assert route.call_count == 1
    assert _close_to(raised.value.retry_at, reset + timedelta(seconds=1))
    assert _rate_limited_count("github") == before + 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_github_secondary_limit_body_is_rate_limited_with_default_wait():
    respx.get(REPO_URL).mock(
        return_value=Response(
            403,
            json={
                "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
            },
        )
    )
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")
    started = datetime.now(timezone.utc)

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider.get_default_branch(repo)

    assert _close_to(raised.value.retry_at, started + timedelta(seconds=61))
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_429_retry_after_beyond_inline_wait_is_raised_with_retry_at():
    route = respx.get(REPO_URL).mock(return_value=Response(429, headers={"Retry-After": "120"}))
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")
    started = datetime.now(timezone.utc)

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider.get_default_branch(repo)

    assert route.call_count == 1
    assert _close_to(raised.value.retry_at, started + timedelta(seconds=121))
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_429_with_short_retry_after_is_retried_inline():
    route = respx.get(REPO_URL).mock(
        side_effect=[
            Response(429, headers={"Retry-After": "0"}),
            Response(200, json={"default_branch": "main"}),
        ]
    )
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    assert await provider.get_default_branch(repo) == "main"
    assert route.call_count == 2
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitlab_ratelimit_reset_header_sets_retry_at():
    reset = datetime.now(timezone.utc) + timedelta(minutes=5)
    respx.get("https://gitlab.com/api/v4/projects/acme%2Fmanifests").mock(
        return_value=Response(
            429,
            json={"message": "Retry later"},
            headers={"RateLimit-Remaining": "0", "RateLimit-Reset": str(int(reset.timestamp()))},
        )
    )
    before = _rate_limited_count("gitlab")
    provider = GitLabProvider("token")
    repo = provider.repository("https://gitlab.com/acme/manifests.git")

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider.get_default_branch(repo)

    assert _close_to(raised.value.retry_at, reset + timedelta(seconds=1))
    assert _rate_limited_count("gitlab") >= before + 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_gitea_x_ratelimit_reset_header_sets_retry_at():
    reset = datetime.now(timezone.utc) + timedelta(minutes=3)
    respx.get("https://gitea.example.com/api/v1/repos/acme/manifests").mock(
        return_value=Response(429, headers={"X-RateLimit-Reset": str(int(reset.timestamp()))})
    )
    provider = GiteaProvider("token", api_base_url="https://gitea.example.com/api/v1")
    repo = provider.repository("https://gitea.example.com/acme/manifests.git")

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider.get_default_branch(repo)

    assert _close_to(raised.value.retry_at, reset + timedelta(seconds=1))
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_plain_403_stays_a_permission_error():
    respx.get(REPO_URL).mock(
        return_value=Response(
            403,
            json={"message": "Resource not accessible by integration"},
            headers={"x-ratelimit-remaining": "4999"},
        )
    )
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    with pytest.raises(GitProviderError) as raised:
        await provider.get_default_branch(repo)

    assert not isinstance(raised.value, GitProviderTransientError)
    assert raised.value.status_code == 403
    await provider.close()


class _CheckRepository:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str | None]] = []

    async def record_provider_check(self, provider_id: str, status: str, *, error_code: str | None = None) -> None:
        self.records.append((provider_id, status, error_code))


@pytest.mark.asyncio
async def test_rate_limit_never_marks_the_provider_permission_denied():
    repository = _CheckRepository()
    checks = ProviderCheckRecorder(repository).bind("github-main")
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    await checks.failed(GitProviderRateLimitError("rate limited", status_code=403, retry_at=retry_at))
    assert repository.records == []

    await checks.failed(GitProviderError("forbidden", status_code=403))
    assert repository.records == [("github-main", "error", "permission_denied")]


def test_redaction_keeps_the_rate_limit_type_and_retry_at():
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    redacted = _redact_provider_error(
        GitProviderRateLimitError("token-123 rate limited", status_code=429, retry_at=retry_at), "token-123"
    )
    assert isinstance(redacted, GitProviderRateLimitError)
    assert redacted.retry_at == retry_at
    assert "token-123" not in str(redacted)


def test_retry_at_is_found_through_the_cause_chain():
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    try:
        try:
            raise GitProviderRateLimitError("limited", status_code=429, retry_at=retry_at)
        except GitProviderRateLimitError as exc:
            raise RuntimeError("wrapped") from exc
    except RuntimeError as wrapped:
        assert rate_limit_retry_at_of(wrapped) == retry_at
    assert rate_limit_retry_at_of(GitProviderTransientError("503")) is None


ARCHIVE_PATH = "/repos/acme/manifests/tarball/" + "a" * 40
ARCHIVE_URL = "https://api.github.com" + ARCHIVE_PATH
SECONDARY_LIMIT_BODY = {
    "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
}


@pytest.mark.asyncio
@respx.mock
async def test_secondary_limit_403_without_headers_is_not_retried_inline():
    route = respx.get(REPO_URL).mock(return_value=Response(403, json=SECONDARY_LIMIT_BODY))
    provider = GitHubProvider("token")
    repo = provider.repository("https://github.com/acme/manifests.git")

    with pytest.raises(GitProviderRateLimitError):
        await provider.get_default_branch(repo)

    # Fast retries extend a secondary-limit penalty: one call, then reschedule.
    assert route.call_count == 1
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_archive_secondary_limit_403_without_headers_is_rate_limited():
    route = respx.get(ARCHIVE_URL).mock(return_value=Response(403, json=SECONDARY_LIMIT_BODY))
    provider = GitHubProvider("token")
    started = datetime.now(timezone.utc)

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider._request_archive(ARCHIVE_PATH, max_bytes=1024)

    assert route.call_count == 1
    assert _close_to(raised.value.retry_at, started + timedelta(seconds=61))
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_archive_403_with_short_retry_after_is_retried_inline():
    route = respx.get(ARCHIVE_URL).mock(
        side_effect=[
            Response(403, json=SECONDARY_LIMIT_BODY, headers={"Retry-After": "1"}),
            Response(200, content=b"archive"),
        ]
    )
    provider = GitHubProvider("token")
    started = datetime.now(timezone.utc)

    content = await provider._request_archive(ARCHIVE_PATH, max_bytes=1024)

    assert content == b"archive"
    assert route.call_count == 2
    # The announced wait was honoured, not the fixed 50 ms backoff.
    assert (datetime.now(timezone.utc) - started).total_seconds() >= 0.9
    await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_archive_short_retry_after_403_ends_rate_limited_never_permanent():
    route = respx.get(ARCHIVE_URL).mock(
        return_value=Response(403, json=SECONDARY_LIMIT_BODY, headers={"Retry-After": "0"})
    )
    provider = GitHubProvider("token")

    with pytest.raises(GitProviderRateLimitError) as raised:
        await provider._request_archive(ARCHIVE_PATH, max_bytes=1024)

    assert raised.value.status_code == 403
    assert route.call_count == 3
    await provider.close()
