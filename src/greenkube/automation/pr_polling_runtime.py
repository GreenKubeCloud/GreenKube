"""Bounded polling runtime for plan-linked pull-request lifecycle fallback."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol
from uuid import uuid4

from greenkube.automation.branch_cleanup_runtime import is_retryable_gitops_error, safe_gitops_error_fields
from greenkube.automation.pr_lifecycle import PullRequestLifecycleOutcome, PullRequestLifecycleResult
from greenkube.core.observability import (
    elapsed,
    record_automation_failure,
    record_pr_poll_cycle,
    record_pr_poll_error,
    record_pr_poll_lease_contention,
    record_pull_request_state,
    start_timer,
)
from greenkube.models.metrics import PullRequestRecord
from greenkube.storage.base_gitops_repository import (
    MAX_PULL_REQUEST_POLLING_LEASE_SECONDS,
    PullRequestPollingLease,
    PullRequestPollingLeaseRepository,
)
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

DEFAULT_GITOPS_PR_POLL_INTERVAL_SECONDS = 120
MIN_GITOPS_PR_POLL_INTERVAL_SECONDS = 10
MAX_GITOPS_PR_POLL_INTERVAL_SECONDS = 86_400
GITOPS_PR_POLL_INTERVAL_ENV = "GITOPS_PR_POLL_INTERVAL"
DEFAULT_GITOPS_PR_POLL_LEASE_SECONDS = 90
DEFAULT_GITOPS_PR_POLL_PROVIDER_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class PollablePullRequestRepository(Protocol):
    """Bounded queue query used by the polling runtime."""

    async def list_pollable_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> list[PullRequestRecord]: ...


class PolledPullRequestReconciler(Protocol):
    """Current-provider-state reconciliation for one linked pull request."""

    async def reconcile_polled(
        self,
        provider_id: str,
        repo: str,
        number: int,
    ) -> PullRequestLifecycleResult: ...


class BranchCleanupWorker(Protocol):
    """Crash-recovery cleanup run while the singleton polling lease is held."""

    async def cleanup_once(
        self,
        *,
        heartbeat: Callable[[], Awaitable[object]] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PullRequestPollingSummary:
    """Secret-free counts from one bounded polling cycle."""

    selected: int = 0
    checked: int = 0
    merged: int = 0
    closed: int = 0
    skipped: int = 0
    errors: int = 0
    lease_acquired: bool = False


def parse_pr_poll_interval_seconds(value: object | None = None) -> int:
    """Parse the polling interval from an explicit value or its environment variable."""
    raw_value = os.environ.get(GITOPS_PR_POLL_INTERVAL_ENV) if value is None else value
    if raw_value is None:
        return DEFAULT_GITOPS_PR_POLL_INTERVAL_SECONDS
    if (
        not isinstance(raw_value, str)
        or not raw_value
        or raw_value != raw_value.strip()
        or not raw_value.isascii()
        or not raw_value.isdecimal()
    ):
        raise ValueError(f"{GITOPS_PR_POLL_INTERVAL_ENV} must be an integer number of seconds.")

    seconds = int(raw_value)
    if not MIN_GITOPS_PR_POLL_INTERVAL_SECONDS <= seconds <= MAX_GITOPS_PR_POLL_INTERVAL_SECONDS:
        raise ValueError(
            f"{GITOPS_PR_POLL_INTERVAL_ENV} must be between "
            f"{MIN_GITOPS_PR_POLL_INTERVAL_SECONDS} and {MAX_GITOPS_PR_POLL_INTERVAL_SECONDS} seconds."
        )
    return seconds


class PullRequestPollingRuntime:
    """Poll a bounded rotating page and delegate terminal transitions to the lifecycle reconciler.

    The queue contains only open/pending PR records linked to a plan. Each cycle
    checks at most ``batch_size`` unique provider references, and the lifecycle
    reconciler independently fetches current provider state before changing
    plan or recommendation state.
    """

    def __init__(
        self,
        pull_request_repository: PollablePullRequestRepository,
        reconciler: PolledPullRequestReconciler,
        lease_repository: PullRequestPollingLeaseRepository,
        *,
        batch_size: int = MAX_PULL_REQUEST_POLL_BATCH_SIZE,
        interval_seconds: int | float | None = None,
        lease_seconds: int = DEFAULT_GITOPS_PR_POLL_LEASE_SECONDS,
        provider_timeout_seconds: int | float = DEFAULT_GITOPS_PR_POLL_PROVIDER_TIMEOUT_SECONDS,
        owner_id: str | None = None,
        branch_cleanup: BranchCleanupWorker | None = None,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_PULL_REQUEST_POLL_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_PULL_REQUEST_POLL_BATCH_SIZE}.")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_PULL_REQUEST_POLLING_LEASE_SECONDS:
            raise ValueError(f"lease_seconds must be between 1 and {MAX_PULL_REQUEST_POLLING_LEASE_SECONDS}.")
        if (
            isinstance(provider_timeout_seconds, bool)
            or not isinstance(provider_timeout_seconds, (int, float))
            or not math.isfinite(provider_timeout_seconds)
            or not 0 < provider_timeout_seconds < lease_seconds
        ):
            raise ValueError("provider_timeout_seconds must be finite, positive, and less than lease_seconds.")
        selected_owner_id = str(uuid4()) if owner_id is None else owner_id
        if not isinstance(selected_owner_id, str) or not selected_owner_id.strip():
            raise ValueError("owner_id must not be blank.")
        self.pull_request_repository = pull_request_repository
        self.reconciler = reconciler
        self.lease_repository = lease_repository
        self.batch_size = batch_size
        self.interval_seconds = _runtime_interval(interval_seconds)
        self.lease_seconds = lease_seconds
        self.provider_timeout_seconds = float(provider_timeout_seconds)
        self.owner_id = selected_owner_id
        self.branch_cleanup = branch_cleanup
        self._after_id: int | None = None
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> PullRequestPollingSummary:
        """Poll one bounded page while holding the database-backed singleton lease."""
        timer = start_timer()
        outcome = "error"
        try:
            async with self._poll_lock:
                lease = await self.lease_repository.try_acquire_pr_polling_lease(
                    self.owner_id,
                    lease_seconds=self.lease_seconds,
                )
                if lease is None:
                    logger.debug("Skipping Git provider pull-request polling because another worker owns the lease.")
                    outcome = "lease_contended"
                    record_pr_poll_lease_contention()
                    return PullRequestPollingSummary(lease_acquired=False)

                try:
                    summary = await self._poll_owned_page(lease)
                finally:
                    released = await self.lease_repository.release_pr_polling_lease(lease)
                    if not released:
                        logger.warning(
                            "Git provider pull-request polling lease was no longer owned at cycle completion."
                        )
                outcome = "completed"
                return summary
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if outcome == "error":
                record_pr_poll_error()
                record_automation_failure("polling")
            record_pr_poll_cycle(outcome, elapsed(timer))

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll immediately, then retry failed cycles after the configured interval."""
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure_fields = safe_gitops_error_fields(exc, stage="pr_poll_cycle")
                logger.warning(
                    "PR polling cycle failed; it will be retried. "
                    "failure_stage=%s failure_code=%s failure_type=%s provider_status_code=%s",
                    failure_fields["failure_stage"],
                    failure_fields["failure_code"],
                    failure_fields["failure_type"],
                    failure_fields["provider_status_code"],
                )
                if not is_retryable_gitops_error(exc):
                    raise RuntimeError("PR polling cycle failed with a non-retryable error.") from None
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def _poll_owned_page(self, lease: PullRequestPollingLease) -> PullRequestPollingSummary:
        records = await self._next_page()
        selected = len(records)
        checked = merged = closed = skipped = errors = 0
        references: set[tuple[str, str, int]] = set()

        for record in records:
            reference = _poll_reference(record)
            if reference is None or reference in references:
                skipped += 1
                continue
            references.add(reference)
            checked += 1
            lease = await self.lease_repository.renew_pr_polling_lease(lease, lease_seconds=self.lease_seconds)
            try:
                result = await asyncio.wait_for(
                    self.reconciler.reconcile_polled(*reference),
                    timeout=self.provider_timeout_seconds,
                )
            except Exception:
                errors += 1
                record_pr_poll_error()
                record_automation_failure("polling")
                logger.warning("Git provider pull-request polling failed for a linked record.")
                continue

            if not isinstance(result, PullRequestLifecycleResult):
                errors += 1
                record_pr_poll_error()
                record_automation_failure("polling")
                logger.warning("Git provider pull-request polling returned an invalid lifecycle result.")
            elif result.outcome is PullRequestLifecycleOutcome.MERGED:
                merged += 1
                record_pull_request_state("merged", record.provider)
            elif result.outcome is PullRequestLifecycleOutcome.CLOSED:
                closed += 1
                record_pull_request_state("closed", record.provider)
            else:
                skipped += 1
                state = "open" if result.outcome is PullRequestLifecycleOutcome.IGNORED else "unchanged"
                record_pull_request_state(state, record.provider)

        if self.branch_cleanup is not None:

            async def renew_cleanup_lease() -> None:
                nonlocal lease
                lease = await self.lease_repository.renew_pr_polling_lease(
                    lease,
                    lease_seconds=self.lease_seconds,
                )

            try:
                await self.branch_cleanup.cleanup_once(heartbeat=renew_cleanup_lease)
            except Exception as exc:
                failure_fields = safe_gitops_error_fields(exc, stage="branch_cleanup_cycle")
                logger.warning(
                    "GitOps plan branch cleanup cycle failed; candidates will be retried. "
                    "failure_stage=%s failure_code=%s failure_type=%s provider_status_code=%s",
                    failure_fields["failure_stage"],
                    failure_fields["failure_code"],
                    failure_fields["failure_type"],
                    failure_fields["provider_status_code"],
                )

        return PullRequestPollingSummary(
            selected=selected,
            checked=checked,
            merged=merged,
            closed=closed,
            skipped=skipped,
            errors=errors,
            lease_acquired=True,
        )

    async def _next_page(self) -> list[PullRequestRecord]:
        records = await self.pull_request_repository.list_pollable_pull_requests(
            limit=self.batch_size,
            after_id=self._after_id,
        )
        if not records and self._after_id is not None:
            self._after_id = None
            records = await self.pull_request_repository.list_pollable_pull_requests(
                limit=self.batch_size,
                after_id=None,
            )

        record_ids = [record.id for record in records if type(record.id) is int and record.id > 0]
        if len(records) == self.batch_size and record_ids:
            self._after_id = max(record_ids)
        else:
            self._after_id = None
        return records


def _runtime_interval(value: int | float | None) -> float:
    if value is None:
        return float(parse_pr_poll_interval_seconds())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("interval_seconds must be a finite positive number.")
    if not MIN_GITOPS_PR_POLL_INTERVAL_SECONDS <= value <= MAX_GITOPS_PR_POLL_INTERVAL_SECONDS:
        raise ValueError(
            f"interval_seconds must be between "
            f"{MIN_GITOPS_PR_POLL_INTERVAL_SECONDS} and {MAX_GITOPS_PR_POLL_INTERVAL_SECONDS}."
        )
    return float(value)


def _poll_reference(record: PullRequestRecord) -> tuple[str, str, int] | None:
    record_id = record.id
    provider_id = record.provider_id
    repo = record.repo
    plan_id = record.plan_id
    number = record.pr_number
    if type(record_id) is not int or record_id <= 0:
        return None
    if not isinstance(provider_id, str) or not isinstance(repo, str) or not isinstance(plan_id, str):
        return None
    if not _valid_token(provider_id) or not _valid_token(plan_id) or not _valid_repository(repo):
        return None
    if type(number) is not int or number <= 0:
        return None
    return provider_id, repo, number


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_repository(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip() or not value:
        return False
    if (
        value.startswith("/")
        or value.endswith("/")
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    parts = value.split("/")
    return len(parts) >= 2 and all(part not in {"", ".", ".."} for part in parts)
