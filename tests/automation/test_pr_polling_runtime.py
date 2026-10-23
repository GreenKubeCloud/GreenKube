import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from greenkube.automation.pr_lifecycle import (
    PullRequestLifecycleOutcome,
    PullRequestLifecycleResult,
)
from greenkube.automation.pr_polling_runtime import (
    DEFAULT_GITOPS_PR_POLL_INTERVAL_SECONDS,
    PullRequestPollingRuntime,
    parse_pr_poll_interval_seconds,
)
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import (
    PullRequestPollingLease,
    PullRequestPollingLeaseNotOwnedError,
    PullRequestPollingLeaseRepository,
)
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

PROVIDER_ID = "provider-github"
REPO = "acme/platform"


def _record(
    record_id: int,
    *,
    provider_id: str | None = PROVIDER_ID,
    repo: str = REPO,
    number: int | None = None,
    plan_id: str | None = "plan",
    status: PullRequestStatus = PullRequestStatus.OPEN,
) -> PullRequestRecord:
    return PullRequestRecord(
        id=record_id,
        recommendation_id=record_id,
        provider="github",
        provider_id=provider_id,
        repo=repo,
        pr_number=number if number is not None else record_id,
        plan_id=plan_id,
        status=status,
    )


class MemoryPollQueue:
    def __init__(self, records: list[PullRequestRecord]) -> None:
        self.records = records
        self.calls: list[tuple[int, int | None]] = []

    async def list_pollable_pull_requests(self, *, limit: int, after_id: int | None = None):
        self.calls.append((limit, after_id))
        records = [
            record
            for record in self.records
            if record.id is not None
            and (after_id is None or record.id > after_id)
            and record.status in {PullRequestStatus.PENDING, PullRequestStatus.OPEN}
            and record.provider_id
            and record.plan_id
            and record.pr_number is not None
        ]
        return records[:limit]


class MemoryPollingLeaseRepository(PullRequestPollingLeaseRepository):
    def __init__(self) -> None:
        self.active_lease: PullRequestPollingLease | None = None
        self.acquire_calls: list[tuple[str, int]] = []
        self.renew_calls: list[PullRequestPollingLease] = []
        self.release_calls: list[PullRequestPollingLease] = []
        self.acquisition_allowed = True
        self.acquire_error: Exception | None = None
        self.renew_error: Exception | None = None
        self.release_error: Exception | None = None
        self.fencing_token = 0

    async def try_acquire_pr_polling_lease(
        self,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease | None:
        self.acquire_calls.append((owner_id, lease_seconds))
        if self.acquire_error is not None:
            raise self.acquire_error
        if not self.acquisition_allowed:
            return None
        now = datetime.now(timezone.utc)
        if self.active_lease is not None and self.active_lease.expires_at > now:
            return None
        self.fencing_token += 1
        self.active_lease = PullRequestPollingLease(
            owner_id=owner_id,
            fencing_token=self.fencing_token,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        return self.active_lease

    async def renew_pr_polling_lease(
        self,
        lease: PullRequestPollingLease,
        *,
        lease_seconds: int,
    ) -> PullRequestPollingLease:
        self.renew_calls.append(lease)
        if self.renew_error is not None:
            raise self.renew_error
        if (
            self.active_lease is None
            or self.active_lease.owner_id != lease.owner_id
            or self.active_lease.fencing_token != lease.fencing_token
            or self.active_lease.expires_at <= datetime.now(timezone.utc)
        ):
            raise PullRequestPollingLeaseNotOwnedError("lease is no longer owned")
        renewed = PullRequestPollingLease(
            owner_id=lease.owner_id,
            fencing_token=lease.fencing_token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        )
        self.active_lease = renewed
        return renewed

    async def release_pr_polling_lease(self, lease: PullRequestPollingLease) -> bool:
        self.release_calls.append(lease)
        if self.release_error is not None:
            raise self.release_error
        if (
            self.active_lease is None
            or self.active_lease.owner_id != lease.owner_id
            or self.active_lease.fencing_token != lease.fencing_token
            or self.active_lease.expires_at <= datetime.now(timezone.utc)
        ):
            return False
        self.active_lease = None
        return True


class MemoryLifecycleReconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.outcomes: dict[tuple[str, str, int], PullRequestLifecycleOutcome] = {}
        self.failures: set[tuple[str, str, int]] = set()
        self.active_calls = 0
        self.max_active_calls = 0
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def reconcile_polled(
        self,
        provider_id: str,
        repo: str,
        number: int,
    ) -> PullRequestLifecycleResult:
        key = (provider_id, repo, number)
        self.calls.append(key)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.started is not None:
                self.started.set()
            if self.release is not None:
                await self.release.wait()
            if key in self.failures:
                raise RuntimeError("private-provider-token")
            outcome = self.outcomes.get(key, PullRequestLifecycleOutcome.IGNORED)
            return PullRequestLifecycleResult(outcome=outcome)
        finally:
            self.active_calls -= 1


class MemoryBranchCleanup:
    def __init__(self) -> None:
        self.calls = 0
        self.heartbeat = None

    async def cleanup_once(self, *, heartbeat=None):
        self.calls += 1
        self.heartbeat = heartbeat
        if heartbeat is not None:
            await heartbeat()


@pytest.mark.asyncio
async def test_poll_once_limits_each_page_and_rotates_through_open_prs():
    queue = MemoryPollQueue([_record(record_id) for record_id in range(1, 5)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    reconciler.outcomes[(PROVIDER_ID, REPO, 1)] = PullRequestLifecycleOutcome.MERGED
    reconciler.outcomes[(PROVIDER_ID, REPO, 2)] = PullRequestLifecycleOutcome.CLOSED
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, batch_size=2, interval_seconds=120)

    first = await runtime.poll_once()
    second = await runtime.poll_once()
    wrapped = await runtime.poll_once()

    assert first.lease_acquired
    assert first.selected == 2
    assert first.checked == 2
    assert first.merged == 1
    assert first.closed == 1
    assert second.selected == 2
    assert wrapped.selected == 2
    assert queue.calls == [(2, None), (2, 2), (2, 4), (2, None)]


@pytest.mark.asyncio
async def test_poll_once_runs_branch_cleanup_while_holding_the_singleton_lease():
    queue = MemoryPollQueue([])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    cleanup = MemoryBranchCleanup()
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, branch_cleanup=cleanup)

    result = await runtime.poll_once()

    assert result.lease_acquired
    assert cleanup.calls == 1
    assert cleanup.heartbeat is not None
    assert len(leases.renew_calls) == 1
    assert len(leases.release_calls) == 1


@pytest.mark.asyncio
async def test_poll_runtime_retries_failed_cycles_without_logging_exception_text(caplog, monkeypatch):
    runtime = PullRequestPollingRuntime(
        MemoryPollQueue([]),
        MemoryLifecycleReconciler(),
        MemoryPollingLeaseRepository(),
    )
    runtime.interval_seconds = 0
    stop_event = asyncio.Event()
    calls = 0

    async def flaky_poll_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database or disk is full; token=never-log-this")
        stop_event.set()

    monkeypatch.setattr(runtime, "poll_once", flaky_poll_once)

    await runtime.run(stop_event)

    assert calls == 2
    assert "failure_stage=pr_poll_cycle" in caplog.text
    assert "failure_code=storage_capacity_exhausted" in caplog.text
    assert "token=never-log-this" not in caplog.text


@pytest.mark.asyncio
async def test_poll_once_deduplicates_provider_references_within_a_batch():
    queue = MemoryPollQueue(
        [
            _record(1, number=40, plan_id="plan-one"),
            _record(2, number=40, plan_id="plan-two"),
            _record(3, number=41),
        ]
    )
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    reconciler.outcomes[(PROVIDER_ID, REPO, 40)] = PullRequestLifecycleOutcome.CLOSED
    reconciler.outcomes[(PROVIDER_ID, REPO, 41)] = PullRequestLifecycleOutcome.MERGED
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, batch_size=3, interval_seconds=120)

    result = await runtime.poll_once()

    assert result.selected == 3
    assert result.checked == 2
    assert result.skipped == 1
    assert reconciler.calls == [(PROVIDER_ID, REPO, 40), (PROVIDER_ID, REPO, 41)]


@pytest.mark.asyncio
async def test_poll_once_hides_provider_errors_and_continues(caplog):
    queue = MemoryPollQueue([_record(1, number=40), _record(2, number=41)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    reconciler.failures.add((PROVIDER_ID, REPO, 40))
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, batch_size=2, interval_seconds=120)

    result = await runtime.poll_once()

    assert result.checked == 2
    assert result.errors == 1
    assert result.skipped == 1
    assert "private-provider-token" not in repr(result)
    assert "private-provider-token" not in caplog.text
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert "greenkube_gitops_pr_poll_errors_total" in output
    assert 'greenkube_automation_failures_total{reason="polling"}' in output
    assert 'greenkube_pull_requests_total{provider="github",state="open"}' in output


@pytest.mark.asyncio
async def test_concurrent_poll_cycles_do_not_reconcile_the_same_pr_concurrently():
    queue = MemoryPollQueue([_record(1)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    reconciler.started = asyncio.Event()
    reconciler.release = asyncio.Event()
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, batch_size=1, interval_seconds=120)

    first = asyncio.create_task(runtime.poll_once())
    await reconciler.started.wait()
    second = asyncio.create_task(runtime.poll_once())
    await asyncio.sleep(0)
    reconciler.release.set()

    await asyncio.gather(first, second)

    assert reconciler.max_active_calls == 1


@pytest.mark.asyncio
async def test_runtime_polls_immediately_and_stops_during_interval_wait():
    stop = asyncio.Event()

    queue = MemoryPollQueue([])
    runtime = PullRequestPollingRuntime(
        queue,
        MemoryLifecycleReconciler(),
        MemoryPollingLeaseRepository(),
        interval_seconds=10,
    )

    async def stop_soon():
        await asyncio.sleep(0.01)
        stop.set()

    stop_task = asyncio.create_task(stop_soon())
    await runtime.run(stop)
    await stop_task

    assert len(queue.calls) == 1


def test_pr_poll_interval_defaults_and_rejects_unsafe_values():
    assert parse_pr_poll_interval_seconds(None) == DEFAULT_GITOPS_PR_POLL_INTERVAL_SECONDS == 120
    assert parse_pr_poll_interval_seconds("300") == 300

    for value in ("0", "9", "-1", "abc", "86401"):
        with pytest.raises(ValueError):
            parse_pr_poll_interval_seconds(value)
    with pytest.raises(ValueError):
        parse_pr_poll_interval_seconds(120)


def test_runtime_enforces_page_and_interval_bounds():
    queue = MemoryPollQueue([])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()

    with pytest.raises(ValueError):
        PullRequestPollingRuntime(queue, reconciler, leases, batch_size=MAX_PULL_REQUEST_POLL_BATCH_SIZE + 1)
    with pytest.raises(ValueError):
        PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=9)


@pytest.mark.asyncio
async def test_non_owner_skips_queue_and_provider_polling():
    queue = MemoryPollQueue([_record(1)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    leases.acquisition_allowed = False
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=120)

    result = await runtime.poll_once()

    assert not result.lease_acquired
    assert result.selected == 0
    assert queue.calls == []
    assert reconciler.calls == []
    assert leases.release_calls == []
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_gitops_pr_poll_cycles_total{outcome="lease_contended"}' in output
    assert "greenkube_gitops_pr_poll_lease_contentions_total" in output


@pytest.mark.asyncio
async def test_poll_renews_lease_before_each_bounded_provider_call():
    queue = MemoryPollQueue([_record(1), _record(2)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=120)

    result = await runtime.poll_once()

    assert result.checked == 2
    assert len(leases.renew_calls) == 2
    assert len(leases.release_calls) == 1


@pytest.mark.asyncio
async def test_poll_provider_call_timeout_is_counted_and_lease_is_released():
    queue = MemoryPollQueue([_record(1)])
    reconciler = MemoryLifecycleReconciler()
    reconciler.started = asyncio.Event()
    reconciler.release = asyncio.Event()
    leases = MemoryPollingLeaseRepository()
    runtime = PullRequestPollingRuntime(
        queue,
        reconciler,
        leases,
        interval_seconds=120,
        lease_seconds=1,
        provider_timeout_seconds=0.01,
    )

    result = await runtime.poll_once()

    assert result.lease_acquired
    assert result.checked == 1
    assert result.errors == 1
    assert len(leases.release_calls) == 1
    assert reconciler.active_calls == 0


@pytest.mark.asyncio
async def test_lease_persistence_errors_fail_closed_and_are_observable(caplog):
    queue = MemoryPollQueue([_record(1)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    leases.acquire_error = RuntimeError("database unavailable")
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=120)

    with pytest.raises(RuntimeError, match="non-retryable"):
        await runtime.run(asyncio.Event())

    assert queue.calls == []
    assert reconciler.calls == []
    assert "failure_stage=pr_poll_cycle" in caplog.text
    assert "failure_code=unexpected_error" in caplog.text
    assert "database unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_lease_renewal_failure_aborts_before_provider_reconciliation():
    queue = MemoryPollQueue([_record(1)])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    leases.renew_error = RuntimeError("lease renewal failed")
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=120)

    with pytest.raises(RuntimeError, match="lease renewal failed"):
        await runtime.poll_once()

    assert queue.calls == [(MAX_PULL_REQUEST_POLL_BATCH_SIZE, None)]
    assert reconciler.calls == []


@pytest.mark.asyncio
async def test_lease_release_persistence_failure_is_not_swallowed(caplog):
    queue = MemoryPollQueue([])
    reconciler = MemoryLifecycleReconciler()
    leases = MemoryPollingLeaseRepository()
    leases.release_error = RuntimeError("lease release failed")
    runtime = PullRequestPollingRuntime(queue, reconciler, leases, interval_seconds=120)

    with pytest.raises(RuntimeError, match="non-retryable"):
        await runtime.run(asyncio.Event())

    assert "failure_stage=pr_poll_cycle" in caplog.text
    assert "failure_code=unexpected_error" in caplog.text
    assert "lease release failed" not in caplog.text
