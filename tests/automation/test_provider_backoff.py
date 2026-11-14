"""Per-record provider backoff for repositories that left the allowlist (removed-repo retry storm)."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

import pytest

from greenkube.automation.branch_cleanup_runtime import BranchCleanupRuntime
from greenkube.automation.git.base import GitProviderError, GitProviderRateLimitError, GitProviderTransientError
from greenkube.automation.git.registry import ProviderAuthorizationError
from greenkube.automation.plan_worker import plan_branch_name
from greenkube.automation.pr_lifecycle import PullRequestLifecycleOutcome, PullRequestLifecycleResult
from greenkube.automation.pr_polling_runtime import PullRequestPollingRuntime
from greenkube.automation.provider_backoff import (
    PROVIDER_BACKOFF_MAX_SECONDS,
    REASON_PROVIDER_UNAUTHORIZED,
    REASON_RATE_LIMITED,
    REASON_REPOSITORY_NOT_ALLOWED,
    ProviderRetryBackoff,
    backoff_delay_seconds,
    classify_provider_failure,
)
from greenkube.gitops.contracts import OperationStatus, Plan, PlanStatus
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import PullRequestPollingLease

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
REPO = "acme/removed"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class PersistingQueue:
    """Poll queue that stores the backoff with the record, like the SQL repositories."""

    def __init__(self, records: list[PullRequestRecord]) -> None:
        self.records = {record.id: record for record in records}
        self.writes: list[tuple[tuple[int, ...], int, datetime | None, str | None]] = []

    async def list_pollable_pull_requests(self, *, limit, after_id=None):
        return [
            record for key, record in sorted(self.records.items()) if after_id is None or cast(int, key) > after_id
        ][:limit]

    async def set_provider_retry_state(self, pr_ids, *, failures, retry_at, detached_reason):
        self.writes.append((tuple(pr_ids), failures, retry_at, detached_reason))
        for pr_id in pr_ids:
            self.records[pr_id] = self.records[pr_id].model_copy(
                update={
                    "provider_retry_failures": failures,
                    "provider_retry_at": retry_at,
                    "provider_detached_reason": detached_reason,
                }
            )

    def release(self) -> None:
        """What adding an allowlist pattern does in storage."""
        for key, record in self.records.items():
            if record.provider_detached_reason is not None:
                self.records[key] = record.model_copy(update={"provider_retry_at": None})


class Reconciler:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.calls = 0

    async def reconcile_polled(self, provider_id, repo, number):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return PullRequestLifecycleResult(PullRequestLifecycleOutcome.IGNORED, "plan-1")


class Leases:
    async def renew_pr_polling_lease(self, lease, *, lease_seconds):
        return lease


LEASE = PullRequestPollingLease(owner_id="test", fencing_token=1, expires_at=T0 + timedelta(days=1))


def _open_record(record_id: int = 1) -> PullRequestRecord:
    return PullRequestRecord(
        id=record_id,
        recommendation_id=record_id,
        provider="github",
        provider_id="github-main",
        repo=REPO,
        pr_number=7,
        plan_id="plan-1",
        status=PullRequestStatus.OPEN,
    )


def _polling(queue, reconciler, clock):
    return PullRequestPollingRuntime(
        queue,
        reconciler,
        cast(object, Leases()),  # type: ignore[arg-type]
        interval_seconds=10,
        lease_seconds=60,
        provider_timeout_seconds=5,
        owner_id="test",
        backoff=ProviderRetryBackoff(queue, clock=clock),
    )


def _not_allowed() -> ProviderAuthorizationError:
    return ProviderAuthorizationError("Repository is not allowed by the Git provider allowlist.")


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def test_backoff_doubles_from_ten_seconds_and_caps_at_one_hour():
    assert [backoff_delay_seconds(step) for step in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert backoff_delay_seconds(9) == 2560
    assert backoff_delay_seconds(10) == PROVIDER_BACKOFF_MAX_SECONDS
    assert backoff_delay_seconds(500) == PROVIDER_BACKOFF_MAX_SECONDS


def test_authorization_and_rate_limit_failures_are_classified_apart():
    retry_at = T0 + timedelta(minutes=5)
    rate_limited = GitProviderRateLimitError("rate limited", status_code=403, retry_at=retry_at)
    assert classify_provider_failure(rate_limited) is not None
    assert classify_provider_failure(rate_limited).reason == REASON_RATE_LIMITED  # type: ignore[union-attr]
    assert classify_provider_failure(rate_limited).retry_at == retry_at  # type: ignore[union-attr]
    assert classify_provider_failure(_not_allowed()).reason == REASON_REPOSITORY_NOT_ALLOWED  # type: ignore[union-attr]
    refused = GitProviderError("bad credentials", status_code=401)
    assert classify_provider_failure(refused).reason == REASON_PROVIDER_UNAUTHORIZED  # type: ignore[union-attr]
    # Transient failures and timeouts keep the per-cycle retry.
    assert classify_provider_failure(GitProviderTransientError("bad gateway", status_code=502)) is None
    assert classify_provider_failure(TimeoutError()) is None


@pytest.mark.asyncio
async def test_removed_repository_backs_off_with_bounded_logs_and_one_detach_warning(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    queue = PersistingQueue([_open_record()])
    reconciler = Reconciler(_not_allowed())
    runtime = _polling(queue, reconciler, clock)

    # Ten minutes of 10-second controller ticks.
    for _ in range(60):
        await runtime.poll_with_lease(LEASE)
        clock.advance(10)

    # Attempts at 0, 10, 30, 70, 150, 310 s: one per backoff step, not one per tick.
    assert reconciler.calls == 6
    failures = [write[1] for write in queue.writes]
    assert failures == [1, 2, 3, 4, 5, 6]
    delays = [(write[2] - T0).total_seconds() for write in queue.writes]  # type: ignore[operator]
    assert delays == [10, 30, 70, 150, 310, 630]
    record = queue.records[1]
    assert record.provider_detached_reason == REASON_REPOSITORY_NOT_ALLOWED
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "no longer allowed" in warnings[0] and REPO in warnings[0]
    audits = [r for r in caplog.records if getattr(r, "audit_event", None) == "pull_request_records_detached"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_reallowlisted_repository_resumes_polling_and_is_reattached(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    queue = PersistingQueue([_open_record()])
    reconciler = Reconciler(_not_allowed())
    runtime = _polling(queue, reconciler, clock)
    for _ in range(4):
        await runtime.poll_with_lease(LEASE)
        clock.advance(10)
    calls_before = reconciler.calls
    assert queue.records[1].provider_retry_at is not None

    # The repository is allowed again: storage releases the detached records.
    reconciler.error = None
    queue.release()
    await runtime.poll_with_lease(LEASE)

    assert reconciler.calls == calls_before + 1
    record = queue.records[1]
    assert (record.provider_retry_failures, record.provider_retry_at, record.provider_detached_reason) == (
        0,
        None,
        None,
    )
    assert any(getattr(r, "audit_event", None) == "pull_request_records_reattached" for r in caplog.records)
    # Normal cadence again: polled on every tick.
    clock.advance(10)
    await runtime.poll_with_lease(LEASE)
    assert reconciler.calls == calls_before + 2


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_at_without_growing_the_backoff_or_detaching():
    clock = Clock()
    queue = PersistingQueue([_open_record()])
    retry_at = T0 + timedelta(seconds=95)
    reconciler = Reconciler(GitProviderRateLimitError("rate limited", status_code=429, retry_at=retry_at))
    runtime = _polling(queue, reconciler, clock)

    await runtime.poll_with_lease(LEASE)
    record = queue.records[1]
    assert record.provider_retry_at == retry_at
    assert record.provider_retry_failures == 0
    assert record.provider_detached_reason is None

    for _ in range(9):
        clock.advance(10)
        await runtime.poll_with_lease(LEASE)
    assert reconciler.calls == 1  # deferred until retry_at (95 s)
    clock.advance(10)
    reconciler.error = None
    await runtime.poll_with_lease(LEASE)
    assert reconciler.calls == 2
    assert queue.records[1].provider_retry_at is None


@pytest.mark.asyncio
async def test_refused_credentials_back_off_with_one_warning_per_step_and_no_detachment(caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    queue = PersistingQueue([_open_record()])
    reconciler = Reconciler(GitProviderError("bad credentials", status_code=401))
    runtime = _polling(queue, reconciler, clock)
    for _ in range(30):
        await runtime.poll_with_lease(LEASE)
        clock.advance(10)

    assert reconciler.calls == 5  # 0, 10, 30, 70, 150 s
    assert len(_warnings(caplog)) == reconciler.calls
    record = queue.records[1]
    assert record.provider_detached_reason is None
    assert record.provider_retry_failures == 5


@pytest.mark.asyncio
async def test_transient_failures_keep_their_per_cycle_retry():
    clock = Clock()
    queue = PersistingQueue([_open_record()])
    reconciler = Reconciler(GitProviderTransientError("bad gateway", status_code=502))
    runtime = _polling(queue, reconciler, clock)
    for _ in range(3):
        await runtime.poll_with_lease(LEASE)
        clock.advance(10)
    assert reconciler.calls == 3
    assert queue.writes == []


class _CleanupRepository:
    def __init__(self, records):
        self.records = {record.id: record for record in records}

    async def list_branch_cleanup_candidates(self, *, limit, after_id=None):
        return [r for k, r in sorted(self.records.items()) if after_id is None or k > after_id][:limit]

    async def list_pull_requests_by_plan_id(self, plan_id):
        return [r for _, r in sorted(self.records.items()) if r.plan_id == plan_id]

    async def mark_plan_branch_cleanup_complete(self, plan_id, *, provider_id, repo, branch_name):
        raise AssertionError("must not complete")

    async def set_provider_retry_state(self, pr_ids, *, failures, retry_at, detached_reason):
        for pr_id in pr_ids:
            self.records[pr_id] = self.records[pr_id].model_copy(
                update={
                    "provider_retry_failures": failures,
                    "provider_retry_at": retry_at,
                    "provider_detached_reason": detached_reason,
                }
            )


class _RefusingResolver:
    def __init__(self):
        self.calls = 0

    async def resolve(self, source):
        self.calls += 1
        raise _not_allowed()


@pytest.mark.asyncio
async def test_branch_cleanup_of_a_removed_repository_backs_off():
    clock = Clock()
    source = SimpleNamespace(provider_id="github-main", repo_url="https://github.com/acme/removed.git", ref="main")
    plan = cast(
        Plan,
        SimpleNamespace(
            plan_id="plan-1",
            digest="sha256:" + "a" * 64,
            recommendation_ids=(1,),
            source=source,
            status=PlanStatus.CLOSED,
        ),
    )
    record = PullRequestRecord(
        id=1,
        recommendation_id=1,
        provider="github",
        provider_id="github-main",
        repo=REPO,
        base_branch="main",
        head_branch=plan_branch_name(plan),
        pr_number=7,
        plan_id="plan-1",
        status=PullRequestStatus.CLOSED,
    )
    repository = _CleanupRepository([record])
    resolver = _RefusingResolver()

    class _Plans:
        async def get_plan(self, plan_id):
            return plan

    class _Operations:
        async def get_by_plan_id(self, plan_id):
            return SimpleNamespace(status=OperationStatus.SUCCEEDED)

    runtime = BranchCleanupRuntime(
        pull_request_repository=repository,
        plan_repository=_Plans(),
        operation_repository=_Operations(),
        provider_resolver=resolver,  # type: ignore[arg-type]
        backoff=ProviderRetryBackoff(repository, clock=clock),
    )
    summaries = []
    for _ in range(30):
        summaries.append(await runtime.cleanup_once())
        clock.advance(10)

    assert resolver.calls == 5  # 0, 10, 30, 70, 150 s
    assert repository.records[1].provider_detached_reason == REASON_REPOSITORY_NOT_ALLOWED
    # Deferred passes select nothing, so they log nothing either.
    assert sum(1 for summary in summaries if summary.selected) == resolver.calls
