import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Sequence, cast

import pytest

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError
from greenkube.automation.plan_worker import PlanWorker
from greenkube.cli import automation_worker
from greenkube.gitops.contracts import PullRequestEvent
from greenkube.storage.base_gitops_repository import WebhookDelivery


class NoopBranchCleanup:
    def __init__(self) -> None:
        self.calls = 0

    async def cleanup_once(self) -> None:
        self.calls += 1


def stub_branch_cleanup(monkeypatch):
    cleanup = NoopBranchCleanup()
    monkeypatch.setattr(automation_worker, "_create_branch_cleanup_runtime", lambda _worker: cleanup)
    return cleanup


@pytest.mark.asyncio
async def test_plans_queue_builds_production_runtime_without_injected_worker(monkeypatch):
    worker_ids: list[str] = []
    recovery_calls: list[tuple[datetime, int]] = []
    from greenkube.core.observability import AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP, AUTOMATION_WORKER_UP

    cleanup = stub_branch_cleanup(monkeypatch)

    class PullRequestRepository:
        async def reconcile_open_pull_requests(self, *, stale_before, limit):
            assert stale_before < datetime.now(timezone.utc)
            assert limit > 0
            recovery_calls.append((stale_before, limit))
            return 2

    class PlanQueue:
        pull_request_repository = PullRequestRepository()

        async def process_once(self, worker_id: str) -> bool:
            assert AUTOMATION_WORKER_UP._value.get() == 1
            worker_ids.append(worker_id)
            return False

    runtime_calls = 0

    async def create_runtime():
        nonlocal runtime_calls
        runtime_calls += 1
        return SimpleNamespace(worker=PlanQueue())

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    await automation_worker.run_worker(once=True, queue="plans")

    assert runtime_calls == 1
    assert worker_ids
    assert AUTOMATION_WORKER_UP._value.get() == 0
    assert AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP._value.get() > 0
    assert cleanup.calls == 1
    assert len(recovery_calls) == 1


@pytest.mark.asyncio
async def test_worker_loop_reuses_one_identity_for_all_claims(monkeypatch):
    worker = cast(PlanWorker, object())
    worker_ids: list[str] = []
    stop_loop = RuntimeError("stop worker loop")

    class Poller:
        async def run(self, stop_event):
            await stop_event.wait()

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert isinstance(worker_id, str)
        worker_ids.append(worker_id)
        if len(worker_ids) == 3:
            raise stop_loop
        return True

    monkeypatch.setattr(automation_worker, "_create_pr_polling_runtime", lambda _worker: Poller())
    monkeypatch.setattr(automation_worker, "process_once", process_once)

    with pytest.raises(RuntimeError, match="stop worker loop"):
        await automation_worker._run_worker_loop(poll_seconds=0.1, plan_worker=worker)

    assert len(worker_ids) == 3
    assert len(set(worker_ids)) == 1


@pytest.mark.asyncio
async def test_worker_reconciles_durable_webhook_outbox_delivery(monkeypatch):
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
            return SimpleNamespace(outcome=SimpleNamespace(value="merged"))

    outbox = Outbox()
    worker = SimpleNamespace(plans=outbox)
    lifecycle = Lifecycle()
    monkeypatch.setattr(automation_worker, "create_pull_request_lifecycle_reconciler", lambda _: lifecycle)

    assert await automation_worker._process_pending_webhook_deliveries(cast(PlanWorker, worker))
    assert outbox.delivery.status == "processed"
    assert lifecycle.calls == 1


@pytest.mark.asyncio
async def test_annotation_based_queue_is_rejected():
    with pytest.raises(ValueError, match="annotation-based operations are no longer supported"):
        await automation_worker.process_once(queue="legacy")


@pytest.mark.asyncio
async def test_plans_queue_safely_idles_when_provider_json_is_absent(monkeypatch):
    async def create_runtime():
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    assert not await automation_worker.process_once(queue="plans")


@pytest.mark.asyncio
async def test_plan_worker_runtime_starts_and_cancels_pr_poller(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    worker = cast(PlanWorker, object())

    class Poller:
        async def run(self, stop_event):
            started.set()
            try:
                await stop_event.wait()
            finally:
                stopped.set()

    def create_poller(plan_worker):
        assert plan_worker is worker
        return Poller()

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert worker_id
        return False

    monkeypatch.setattr(automation_worker, "_create_pr_polling_runtime", create_poller)
    monkeypatch.setattr(automation_worker, "process_once", process_once)
    task = asyncio.create_task(automation_worker.run_worker(poll_seconds=10, queue="plans", plan_worker=worker))

    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_plan_worker_surfaces_pr_poller_persistence_failures(monkeypatch):
    worker = cast(PlanWorker, object())

    class Poller:
        async def run(self, _stop_event):
            raise RuntimeError("polling lease database unavailable")

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert worker_id
        return False

    monkeypatch.setattr(automation_worker, "_create_pr_polling_runtime", lambda _worker: Poller())
    monkeypatch.setattr(automation_worker, "process_once", process_once)

    with pytest.raises(RuntimeError, match="polling lease database unavailable"):
        await automation_worker.run_worker(poll_seconds=10, queue="plans", plan_worker=worker)


@pytest.mark.asyncio
async def test_dedicated_worker_supervises_metrics_listener_without_changing_queue_loop(monkeypatch):
    process_started = asyncio.Event()
    worker_metrics_ports: list[int] = []
    worker = cast(PlanWorker, object())
    stub_branch_cleanup(monkeypatch)

    class MetricsServer:
        started = True
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    server = MetricsServer()

    def create_metrics_server(port: int):
        worker_metrics_ports.append(port)
        return server

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert worker_id
        process_started.set()
        return False

    monkeypatch.setattr(automation_worker, "create_worker_metrics_server", create_metrics_server)
    monkeypatch.setattr(automation_worker, "process_once", process_once)
    await automation_worker.run_worker(
        poll_seconds=10,
        once=True,
        queue="plans",
        plan_worker=worker,
        metrics_port=8123,
    )

    assert process_started.is_set()
    assert worker_metrics_ports == [8123]
    assert server.should_exit


def test_worker_metrics_port_is_optional_and_bounded(monkeypatch):
    monkeypatch.delenv(automation_worker.WORKER_METRICS_PORT_ENV, raising=False)

    assert automation_worker.parse_worker_metrics_port() is None
    assert automation_worker.parse_worker_metrics_port("8001") == 8001
    monkeypatch.setenv(automation_worker.WORKER_METRICS_PORT_ENV, "8123")
    assert automation_worker.parse_worker_metrics_port() == 8123

    for value in ("", "0", "65536", "port", " 8001"):
        with pytest.raises(ValueError):
            automation_worker.parse_worker_metrics_port(value)


def test_pr_poller_uses_plan_gitops_repository_for_database_lease(monkeypatch):
    pull_requests = object()
    gitops_repository = object()
    worker = cast(
        PlanWorker,
        SimpleNamespace(
            pull_request_repository=pull_requests,
            plans=gitops_repository,
            operations=object(),
            provider_resolver=object(),
        ),
    )
    reconciler = object()
    monkeypatch.setattr(automation_worker, "create_pull_request_lifecycle_reconciler", lambda _worker: reconciler)

    poller = automation_worker._create_pr_polling_runtime(worker)

    assert poller.pull_request_repository is pull_requests
    assert poller.lease_repository is gitops_repository
