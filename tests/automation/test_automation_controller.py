"""The automation controller loop and the controller plan executor (roadmap M8, F8)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Sequence, cast
from uuid import uuid4

import asyncpg
import pytest

from greenkube.automation import controller as controller_module
from greenkube.automation.controller import (
    PLAN_HISTORY_RETENTION_DAYS,
    AutomationController,
    PlanExecutor,
    create_pr_polling_runtime,
    process_pending_webhook_deliveries,
)
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.leader_election import LeaderFence, LeadershipLostError, leader_fence_scope
from greenkube.core.loops import LoopRegistry, LoopSpec
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import PullRequestPollingLease, WebhookDelivery


class _Clock:
    def __init__(self) -> None:
        self.mono = 1_000.0
        self.now = datetime.now(timezone.utc)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


class _Leases:
    """In-memory singleton lease with the repository's owner/fencing semantics."""

    def __init__(self, *, holder: str | None = None) -> None:
        self.holder = holder
        self.token = 0
        self.events: list[str] = []

    async def try_acquire_pr_polling_lease(self, owner_id, *, lease_seconds):
        if self.holder is not None:
            return None
        self.holder = owner_id
        self.token += 1
        self.events.append("acquire")
        return PullRequestPollingLease(
            owner_id=owner_id,
            fencing_token=self.token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        )

    async def renew_pr_polling_lease(self, lease, *, lease_seconds):
        assert self.holder == lease.owner_id
        self.events.append("renew")
        return lease

    async def release_pr_polling_lease(self, lease):
        released = self.holder == lease.owner_id
        self.holder = None
        self.events.append("release")
        return released


class _PlanService:
    def __init__(self, calls: list[str], *, fail: set[str] | None = None) -> None:
        self.calls = calls
        self.fail = fail or set()

    async def reconcile_exhausted_operations(self):
        self.calls.append("operations")
        if "operations" in self.fail:
            raise ConnectionError("database restarting")
        return []

    async def expire_due_plans(self):
        self.calls.append("expiry")
        return ["stale-plan"]

    async def prune_retained_history(self, *, cutoff):
        self.calls.append("retention")
        if "retention" in self.fail:
            raise ConnectionError("database restarting")
        self.cutoff = cutoff
        return {"plans": 1}


def _controller(calls, *, delivery=False, fail=None, leases=None, clock=None, polling=None, worker=None):
    clock = clock or _Clock()
    return AutomationController(
        plan_service=cast(Any, _PlanService(calls, fail=fail)),
        lease_repository=cast(Any, leases or _Leases()),
        plan_worker=worker,
        pull_request_polling=polling,
        delivery_enabled=delivery,
        pr_poll_interval_seconds=120,
        clock=lambda: clock.now,
        monotonic=lambda: clock.mono,
    )


class _Polling:
    interval_seconds = 120.0

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def poll_with_lease(self, lease):
        assert isinstance(lease, PullRequestPollingLease)
        self.calls.append("pr_polling")


class _PullRequests:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def reconcile_open_pull_requests(self, *, stale_before, limit):
        self.calls.append("recovery")
        return 0


class _WorkerPlans:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def list_pending_webhook_deliveries(self, *, limit, stale_before):
        self.calls.append("webhooks")
        return []

    async def prune_worker_heartbeats(self, *, cutoff):
        self.calls.append("heartbeats")
        return 0


def _worker(calls):
    return SimpleNamespace(plans=_WorkerPlans(calls), pull_request_repository=_PullRequests(calls))


@pytest.mark.asyncio
async def test_tick_runs_every_step_under_one_lease_then_releases_it():
    calls: list[str] = []
    leases = _Leases()
    controller = _controller(calls, delivery=True, leases=leases, polling=_Polling(calls), worker=_worker(calls))

    summary = await controller.tick()

    assert summary.lease_acquired
    assert calls == [
        "operations",
        "expiry",
        "webhooks",
        "pr_polling",
        "recovery",
        "retention",
        "heartbeats",
    ]
    assert leases.events[0] == "acquire" and leases.events[-1] == "release"
    assert leases.events.count("acquire") == 1
    assert leases.holder is None
    assert cast(Any, controller.plan_service).cutoff <= datetime.now(timezone.utc) - timedelta(
        days=PLAN_HISTORY_RETENTION_DAYS
    )


@pytest.mark.asyncio
async def test_tick_does_nothing_while_another_process_holds_the_lease():
    calls: list[str] = []
    controller = _controller(calls, leases=_Leases(holder="automation-controller:other"))

    summary = await controller.tick()

    assert not summary.lease_acquired
    assert calls == []


@pytest.mark.asyncio
async def test_steps_keep_their_own_cadence_inside_the_fast_tick():
    calls: list[str] = []
    clock = _Clock()
    controller = _controller(calls, delivery=True, clock=clock, polling=_Polling(calls), worker=_worker(calls))

    await controller.tick()
    calls.clear()
    clock.advance(5)
    await controller.tick()
    # Only the webhook outbox runs every tick; recovery runs once per process.
    assert calls == ["webhooks"]

    calls.clear()
    clock.advance(60)
    await controller.tick()
    assert calls == ["operations", "expiry", "webhooks"]

    calls.clear()
    clock.advance(60)
    await controller.tick()
    assert calls == ["operations", "expiry", "webhooks", "pr_polling"]

    calls.clear()
    clock.advance(86_400)
    await controller.tick()
    assert "retention" in calls and "recovery" not in calls


@pytest.mark.asyncio
async def test_without_delivery_only_expiry_and_retention_run():
    """The old API sweep ran whenever the plan runtime existed; delivery steps need automation on."""
    calls: list[str] = []
    controller = _controller(calls, delivery=False, polling=_Polling(calls), worker=_worker(calls))

    await controller.tick()

    assert calls == ["operations", "expiry", "retention"]


@pytest.mark.asyncio
async def test_a_failed_step_does_not_stop_the_others_and_fails_the_loop_tick():
    calls: list[str] = []
    controller = _controller(calls, fail={"operations"})
    registry = LoopRegistry()
    registry.register(LoopSpec("automation-controller", controller.tick, 5))

    assert await registry.run_once("automation-controller") == "failure"

    assert calls == ["operations", "expiry", "retention"]
    assert registry.status("automation-controller").last_error_code == "operations_failed"


@pytest.mark.asyncio
async def test_a_failed_retention_pass_is_retried_on_the_next_tick():
    calls: list[str] = []
    clock = _Clock()
    controller = _controller(calls, fail={"retention"}, clock=clock)

    await controller.tick()
    calls.clear()
    clock.advance(5)
    cast(Any, controller.plan_service).fail.clear()
    await controller.tick()

    assert calls == ["retention"]


@pytest.mark.asyncio
async def test_a_controller_that_lost_the_kubernetes_lease_stops_before_the_next_step():
    class _Elector:
        def __init__(self):
            self.fencing_token = 1
            self.is_leader = True

    elector = _Elector()
    calls: list[str] = []
    leases = _Leases()
    controller = _controller(calls, leases=leases)

    async def lose_lease_then_expire():
        calls.append("expiry")
        elector.fencing_token = 2
        return []

    controller.plan_service.expire_due_plans = lose_lease_then_expire  # type: ignore[method-assign]
    with leader_fence_scope(LeaderFence(elector)):  # type: ignore[arg-type]
        with pytest.raises(LeadershipLostError):
            await controller.tick()

    assert calls == ["operations", "expiry"]
    assert leases.holder is None, "the DB lease is released even when the tick aborts"


# -- plan executor -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_executor_drains_a_bounded_batch_with_one_identity():
    from greenkube.core.observability import AUTOMATION_WORKER_UP

    worker_ids: list[str] = []

    class Worker:
        async def process_once(self, worker_id):
            worker_ids.append(worker_id)
            return True

    executor = PlanExecutor(Worker(), max_operations_per_tick=3)

    assert await executor.tick() == 3
    assert AUTOMATION_WORKER_UP._value.get() == 1
    assert len(set(worker_ids)) == 1
    executor.close()
    assert AUTOMATION_WORKER_UP._value.get() == 0


@pytest.mark.asyncio
async def test_plan_executor_stops_claiming_when_the_queue_is_empty():
    class Worker:
        calls = 0

        async def process_once(self, worker_id):
            self.calls += 1
            return self.calls < 2

    worker = Worker()
    assert await PlanExecutor(worker).tick() == 1
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_plan_executor_does_not_claim_after_losing_the_lease():
    class _Elector:
        fencing_token = 1
        is_leader = True

    elector = _Elector()

    class Worker:
        calls = 0

        async def process_once(self, worker_id):
            self.calls += 1
            elector.fencing_token = 2
            return True

    worker = Worker()
    with leader_fence_scope(LeaderFence(elector)):  # type: ignore[arg-type]
        with pytest.raises(LeadershipLostError):
            await PlanExecutor(worker).tick()

    assert worker.calls == 1


# -- composition -----------------------------------------------------------------------------


def test_pr_poller_uses_plan_gitops_repository_for_database_lease(monkeypatch):
    pull_requests = object()
    gitops_repository = object()
    worker = SimpleNamespace(
        pull_request_repository=pull_requests,
        plans=gitops_repository,
        operations=object(),
        provider_resolver=object(),
    )
    reconciler = object()
    monkeypatch.setattr(controller_module, "create_pull_request_lifecycle_reconciler", lambda _worker: reconciler)

    poller = create_pr_polling_runtime(worker)

    assert poller.pull_request_repository is pull_requests
    assert poller.lease_repository is gitops_repository
    assert poller.branch_cleanup is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error"),
    [
        ("merged", "processed", None),
        # D11 / BUG-53: unknown pull requests become terminal instead of retrying forever.
        ("unlinked", "ignored", "pull_request_unlinked"),
    ],
)
async def test_controller_reconciles_durable_webhook_outbox_delivery(
    monkeypatch, outcome, expected_status, expected_error
):
    event = PullRequestEvent(
        provider_id="provider-main",
        delivery_id="delivery-1",
        repo="acme/platform",
        number=42,
        action="closed",
        merged=True,
        merge_commit_sha="a" * 40,
    )

    class Outbox:
        def __init__(self) -> None:
            self.delivery = WebhookDelivery(
                id=1,
                event_type="pull_request",
                event=event,
                status="retryable",
                received_at=datetime.now(timezone.utc),
                provider_id=event.provider_id,
                delivery_id=event.delivery_id,
            )

        async def list_pending_webhook_deliveries(self, *, limit: int, stale_before: datetime):
            return [self.delivery] if self.delivery.status == "retryable" else []

        async def claim_webhook_delivery(self, provider_id: str, delivery_id: str, *, stale_before: datetime):
            if (provider_id, delivery_id) != (event.provider_id, event.delivery_id):
                return False
            if self.delivery.status != "retryable":
                return False
            self.delivery = self.delivery.__class__(
                id=self.delivery.id,
                event_type=self.delivery.event_type,
                event=self.delivery.event,
                status="processing",
                received_at=self.delivery.received_at,
                provider_id=provider_id,
                delivery_id=delivery_id,
            )
            return True

        async def update_webhook_delivery(
            self,
            provider_id: str,
            delivery_id: str,
            *,
            status: str,
            processed_at=None,
            error_code=None,
            expected_statuses: Sequence[str] | None = None,
        ) -> WebhookDelivery | None:
            assert expected_statuses is not None
            assert self.delivery.status in expected_statuses
            self.delivery = self.delivery.__class__(
                id=self.delivery.id,
                event_type=self.delivery.event_type,
                event=self.delivery.event,
                status=status,
                received_at=self.delivery.received_at,
                processed_at=processed_at,
                error_code=error_code,
                provider_id=provider_id,
                delivery_id=delivery_id,
            )
            return self.delivery

    class Lifecycle:
        calls = 0

        async def reconcile(self, received_event, *, newly_deduplicated):
            self.calls += 1
            assert received_event == event
            assert newly_deduplicated is True
            return SimpleNamespace(outcome=SimpleNamespace(value=outcome))

    outbox = Outbox()
    worker = SimpleNamespace(plans=outbox)
    lifecycle = Lifecycle()
    monkeypatch.setattr(controller_module, "create_pull_request_lifecycle_reconciler", lambda _: lifecycle)

    assert await process_pending_webhook_deliveries(worker)
    assert outbox.delivery.status == expected_status
    assert outbox.delivery.error_code == expected_error
    assert outbox.delivery.processed_at is not None
    assert lifecycle.calls == 1
    assert not await process_pending_webhook_deliveries(worker)


# -- real database lease ------------------------------------------------------------------------


@pytest.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
async def lease_store(request, monkeypatch):
    from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository
    from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository

    backend = request.param
    postgres_dsn = None
    schema = None
    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run live PostgreSQL lease tests.")
        schema = f"gk_automation_ctrl_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", ":memory:")
        monkeypatch.setenv("DB_SCHEMA", "public")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "4")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    repository = PostgresGitOpsRepository(manager) if backend == "postgres" else SQLiteGitOpsRepository(manager)
    try:
        yield repository
    finally:
        await manager.close()
        config.reload()
        if postgres_dsn is not None and schema is not None:
            connection = await asyncpg.connect(postgres_dsn)
            try:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()


@pytest.mark.asyncio
async def test_two_controllers_never_run_the_automation_tick_concurrently(lease_store):
    """Without Kubernetes leader election (standalone replicas) the DB lease is the guard."""
    inside = 0
    overlap = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class _SlowService:
        async def reconcile_exhausted_operations(self):
            nonlocal inside, overlap
            inside += 1
            overlap = max(overlap, inside)
            entered.set()
            await release.wait()
            inside -= 1
            return []

        async def expire_due_plans(self):
            return []

        async def prune_retained_history(self, *, cutoff):
            return {}

    first = AutomationController(plan_service=cast(Any, _SlowService()), lease_repository=cast(Any, lease_store))
    second = AutomationController(plan_service=cast(Any, _SlowService()), lease_repository=cast(Any, lease_store))

    running = asyncio.create_task(first.tick())
    await entered.wait()
    contended = await second.tick()
    release.set()
    completed = await running

    assert completed.lease_acquired and completed.steps["expiry"] == "ok"
    assert not contended.lease_acquired
    assert overlap == 1
    # Released at the end of the tick: the other controller takes the next one.
    assert (await second.tick()).lease_acquired
