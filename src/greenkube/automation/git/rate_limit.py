# src/greenkube/automation/git/rate_limit.py
"""Git provider rate-limit classification.

A rate limit is transient: the same request succeeds once the provider's window
resets. Providers signal it differently:

- **GitHub**: ``429``, or ``403`` with ``x-ratelimit-remaining: 0`` (primary limit) or a
  "secondary rate limit" message, with ``retry-after`` (seconds) or
  ``x-ratelimit-reset`` (epoch seconds);
- **GitLab**: ``429`` with ``Retry-After`` and ``RateLimit-Reset`` (epoch seconds);
- **Gitea** (and reverse proxies in front of any provider): ``429`` with ``Retry-After``
  or ``X-RateLimit-Reset``.

A rate-limited response is never an authorization failure: the token is valid, so the
provider must not be marked ``permission_denied``, and plan operations are rescheduled
at the provider's reset time instead of failing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import httpx
from prometheus_client import Counter

from greenkube.core.observability import REGISTRY

#: Wait used when a rate-limited response names no reset time (GitHub asks for at least a minute).
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60
#: Upper bound on a provider-announced wait; every documented window resets within an hour.
MAX_RATE_LIMIT_WAIT_SECONDS = 3600
#: Longest wait a single provider request sleeps through; longer waits reschedule the caller.
MAX_INLINE_RATE_LIMIT_WAIT_SECONDS = 2.0

_EPOCH_THRESHOLD = 1_000_000_000
_RATE_LIMIT_MESSAGES = ("rate limit", "ratelimit", "too many requests", "abuse detection")
_PROVIDERS = frozenset({"github", "gitlab", "gitea"})

GIT_PROVIDER_RATE_LIMITED = Counter(
    "greenkube_git_provider_rate_limited_total",
    "Git provider API responses that signalled a rate limit (429, or 403 with an exhausted quota).",
    ["provider"],
    registry=REGISTRY,
)


def record_rate_limited(provider: str) -> None:
    """Count one rate-limited provider response."""
    GIT_PROVIDER_RATE_LIMITED.labels(provider=provider if provider in _PROVIDERS else "other").inc()


def is_rate_limited(response: httpx.Response) -> bool:
    """Whether ``response`` is a provider rate limit rather than an authorization failure."""
    status = response.status_code
    if status == 429:
        return True
    if status != 403:
        return False
    headers = response.headers
    for name in ("x-ratelimit-remaining", "ratelimit-remaining"):
        value = headers.get(name)
        if value is not None and value.strip() == "0":
            return True
    if headers.get("retry-after") is not None:
        return True
    try:
        text = response.text[:2000].lower()
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        return False
    return any(message in text for message in _RATE_LIMIT_MESSAGES)


def announced_wait_seconds(headers: Mapping[str, str], *, now: datetime) -> float | None:
    """Seconds until the provider says a retry may succeed, or ``None`` when it does not say."""
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        value = retry_after.strip()
        try:
            return max(float(value), 0.0)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max((parsed - now).total_seconds(), 0.0)
    for name in ("x-ratelimit-reset", "ratelimit-reset"):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            continue
        if value >= _EPOCH_THRESHOLD:
            return max(value - now.timestamp(), 0.0)
        return max(value, 0.0)
    return None


def rate_limit_retry_at(response: httpx.Response, *, now: datetime | None = None) -> datetime | None:
    """When a rate-limited ``response`` may be retried; ``None`` when it is not rate limited."""
    if not is_rate_limited(response):
        return None
    current = now or datetime.now(timezone.utc)
    wait = announced_wait_seconds(response.headers, now=current)
    if wait is None:
        wait = DEFAULT_RATE_LIMIT_WAIT_SECONDS
    # Add one second: an epoch reset is rounded down and the window closes on the boundary.
    wait = min(max(wait, 0.0) + 1.0, MAX_RATE_LIMIT_WAIT_SECONDS)
    return current + timedelta(seconds=wait)


def rate_limit_retry_at_of(error: BaseException | None) -> datetime | None:
    """The ``retry_at`` of the first rate-limit error in ``error``'s cause chain, if any."""
    from greenkube.automation.git.base import GitProviderRateLimitError

    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GitProviderRateLimitError):
            return current.retry_at
        current = current.__cause__ or current.__context__
    return None
