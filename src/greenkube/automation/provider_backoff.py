"""Per-record provider backoff for plan-linked pull requests.

PR polling, plan branch cleanup and the lifecycle comment call the Git provider for
every plan pull-request record on every cycle. When a repository leaves the provider
allowlist, or the provider credentials no longer authorize it, every one of those
calls fails the same way; retrying them every cycle only hammers the provider (or the
registry) and floods the logs. This module keeps a capped exponential backoff per
record, stored with the record (``provider_retry_*`` columns, migration 0113):

* **Rate limit** (:class:`~greenkube.automation.git.base.GitProviderRateLimitError`
  anywhere in the cause chain): the record is deferred until the provider's
  ``retry_at``. It is not a failed attempt, so the backoff step does not grow.
* **Repository no longer allowlisted** (``ProviderAuthorizationError``): the records
  are *detached* (``provider_detached_reason = repository_not_allowed``) with one
  warning and an audit event, then retried at the backoff cadence only. Adding an
  allowlist pattern makes them due at once; the first successful call reattaches them.
* **Credentials refused** (HTTP 401/403) or provider deleted/disabled: backoff, one
  warning per step.

Any other failure keeps the callers' existing handling (retried next cycle).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from greenkube.automation.git.base import GitProviderError, GitProviderTransientError
from greenkube.automation.git.rate_limit import rate_limit_retry_at_of
from greenkube.automation.git.registry import (
    GitProviderConfigurationError,
    ProviderAuthorizationError,
    UnknownGitProviderError,
)
from greenkube.core.observability import audit_pull_request_detachment
from greenkube.models.metrics import PullRequestRecord

logger = logging.getLogger(__name__)

PROVIDER_BACKOFF_BASE_SECONDS = 10.0
PROVIDER_BACKOFF_MAX_SECONDS = 3600.0

REASON_RATE_LIMITED = "rate_limited"
REASON_REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
REASON_PROVIDER_UNAUTHORIZED = "provider_unauthorized"
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """How a provider failure is retried: until ``retry_at`` (rate limit) or by backoff."""

    reason: str
    retry_at: datetime | None = None


def classify_provider_failure(error: BaseException | None) -> ProviderFailure | None:
    """Classify a failure that must not be retried every cycle, or ``None``.

    The rate-limit classification of the provider-resilience wave wins: a
    rate-limited 403 is never an authorization failure.
    """
    if error is None:
        return None
    retry_at = rate_limit_retry_at_of(error)
    if retry_at is not None:
        return ProviderFailure(REASON_RATE_LIMITED, retry_at)
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProviderAuthorizationError):
            return ProviderFailure(REASON_REPOSITORY_NOT_ALLOWED)
        if isinstance(current, (UnknownGitProviderError, GitProviderConfigurationError)):
            return ProviderFailure(REASON_PROVIDER_UNAVAILABLE)
        if (
            isinstance(current, GitProviderError)
            and not isinstance(current, GitProviderTransientError)
            and getattr(current, "status_code", None) in (401, 403)
        ):
            return ProviderFailure(REASON_PROVIDER_UNAUTHORIZED)
        current = current.__cause__ or current.__context__
    return None


def backoff_delay_seconds(failures: int) -> float:
    """10 s, 20 s, 40 s ... capped at one hour, for the ``failures``-th consecutive failure."""
    if failures <= 0:
        return 0.0
    exponent = min(failures - 1, 32)
    return min(PROVIDER_BACKOFF_MAX_SECONDS, PROVIDER_BACKOFF_BASE_SECONDS * (2**exponent))


@dataclass(frozen=True, slots=True)
class _State:
    failures: int = 0
    retry_at: datetime | None = None
    detached_reason: str | None = None


class ProviderRetryBackoff:
    """Defer, record and clear the provider backoff of plan pull-request records."""

    def __init__(
        self,
        repository: object,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.clock = clock
        # Fallback for repositories that cannot persist the state.
        self._states: dict[int, _State] = {}
        self._memory_only = not callable(getattr(repository, "set_provider_retry_state", None))

    def deferred(self, records: Sequence[PullRequestRecord]) -> bool:
        """Whether any of ``records`` is still inside its backoff or rate-limit window."""
        now = self.clock()
        return any(
            (retry_at := self._state(record).retry_at) is not None and _aware(retry_at) > now for record in records
        )

    async def failed(self, records: Sequence[PullRequestRecord], error: BaseException, *, loop: str) -> bool:
        """Record a provider failure; ``False`` when it is not a backoff-class failure."""
        failure = classify_provider_failure(error)
        ids = _record_ids(records)
        if failure is None or not ids:
            return False
        previous = max((self._state(record) for record in records), key=lambda state: state.failures)
        first = records[0]
        now = self.clock()
        if failure.reason == REASON_RATE_LIMITED:
            assert failure.retry_at is not None
            state = _State(previous.failures, max(_aware(failure.retry_at), now), previous.detached_reason)
            logger.warning(
                "Git provider rate limit reached for plan pull-request records; %s is deferred until %s. "
                "provider_id=%s repo=%s record_ids=%s",
                loop,
                state.retry_at.isoformat() if state.retry_at else None,
                first.provider_id,
                first.repo,
                ids,
            )
        else:
            failures = previous.failures + 1
            delay = backoff_delay_seconds(failures)
            detached = failure.reason if failure.reason == REASON_REPOSITORY_NOT_ALLOWED else None
            state = _State(failures, now + timedelta(seconds=delay), detached)
            if detached is not None and previous.detached_reason != detached:
                logger.warning(
                    "Repository %s is no longer allowed for Git provider %s: its plan pull-request records %s "
                    "are detached and no longer polled. Add the repository back to the allowlist to resume.",
                    first.repo,
                    first.provider_id,
                    ids,
                )
                audit_pull_request_detachment(
                    "pull_request_records_detached",
                    provider_id=first.provider_id,
                    repo=first.repo,
                    record_ids=list(ids),
                    reason=detached,
                )
            elif detached is not None:
                logger.debug(
                    "Detached plan pull-request records %s are still not allowed; next check in %ds.",
                    ids,
                    int(delay),
                )
            else:
                logger.warning(
                    "Git provider refused plan pull-request records (%s); %s backs off for %ds "
                    "(consecutive failures: %d). provider_id=%s repo=%s record_ids=%s",
                    failure.reason,
                    loop,
                    int(delay),
                    failures,
                    first.provider_id,
                    first.repo,
                    ids,
                )
        await self._store(ids, state)
        return True

    async def succeeded(self, records: Sequence[PullRequestRecord]) -> None:
        """Clear the backoff after a successful provider call; reattach detached records."""
        ids = _record_ids(records)
        states = [self._state(record) for record in records]
        if not ids or all(state == _State() for state in states):
            return
        detached = next((state.detached_reason for state in states if state.detached_reason), None)
        await self._store(ids, _State())
        if detached is not None:
            first = records[0]
            logger.info(
                "Repository %s is allowed again for Git provider %s: plan pull-request records %s are reattached.",
                first.repo,
                first.provider_id,
                ids,
            )
            audit_pull_request_detachment(
                "pull_request_records_reattached",
                provider_id=first.provider_id,
                repo=first.repo,
                record_ids=list(ids),
                reason=detached,
            )

    def _state(self, record: PullRequestRecord) -> _State:
        persisted = _State(
            int(getattr(record, "provider_retry_failures", 0) or 0),
            getattr(record, "provider_retry_at", None),
            getattr(record, "provider_detached_reason", None),
        )
        if not self._memory_only:
            # Records are read fresh every cycle: the stored state is the truth, so a
            # release (allowlist widened) or a clear by another loop is seen at once.
            return persisted
        record_id = record.id
        overlay = self._states.get(record_id) if type(record_id) is int else None
        return persisted if overlay is None else overlay

    async def _store(self, ids: tuple[int, ...], state: _State) -> None:
        if not self._memory_only:
            try:
                await self.repository.set_provider_retry_state(  # type: ignore[attr-defined]
                    ids,
                    failures=state.failures,
                    retry_at=state.retry_at,
                    detached_reason=state.detached_reason,
                )
                return
            except NotImplementedError:
                self._memory_only = True
            except Exception as error:
                logger.warning(
                    "Could not persist the provider backoff of plan pull-request records (%s).",
                    type(error).__name__,
                )
                return
        # In-memory fallback for repositories without the provider backoff columns.
        for record_id in ids:
            if state == _State():
                self._states.pop(record_id, None)
            else:
                self._states[record_id] = state


def _record_ids(records: Sequence[PullRequestRecord]) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(record.id for record in records if type(record.id) is int and record.id > 0)  # type: ignore[misc]
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
