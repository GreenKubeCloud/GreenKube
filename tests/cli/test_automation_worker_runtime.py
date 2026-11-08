import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Sequence, cast

import pytest
from typer.testing import CliRunner

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

    await automation_worker.run_worker(once=True)

    assert runtime_calls == 1
    assert worker_ids
    assert AUTOMATION_WORKER_UP._value.get() == 0
    assert AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP._value.get() > 0
    assert cleanup.calls == 1
    assert len(recovery_calls) == 1


class _StopLoop(BaseException):
    """Escapes the supervised loop, which deliberately survives every ``Exception``."""


@pytest.mark.asyncio
async def test_worker_loop_reuses_one_identity_for_all_claims(monkeypatch):
    worker = cast(PlanWorker, object())
    worker_ids: list[str] = []
    stop_loop = _StopLoop("stop worker loop")

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

    with pytest.raises(_StopLoop, match="stop worker loop"):
        await automation_worker._run_worker_loop(poll_seconds=0.1, plan_worker=worker)

    assert len(worker_ids) == 3
    assert len(set(worker_ids)) == 1


@pytest.mark.asyncio
async def test_worker_iteration_failures_are_logged_retried_and_heartbeat(monkeypatch, caplog):
    """D9 / BUG-51: one failed iteration used to end the queue loop silently."""
    worker = cast(PlanWorker, object())
    calls = 0
    heartbeats = 0
    delays: list[float] = []

    class Poller:
        async def run(self, stop_event):
            await stop_event.wait()

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("database is restarting")
        if calls == 4:
            raise _StopLoop()
        return True

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(automation_worker, "_create_pr_polling_runtime", lambda _worker: Poller())
    monkeypatch.setattr(automation_worker, "process_once", process_once)
    monkeypatch.setattr(automation_worker.asyncio, "sleep", fast_sleep)

    with caplog.at_level("ERROR", logger="greenkube.cli.automation_worker"):
        with pytest.raises(_StopLoop):
            await automation_worker._run_worker_loop(poll_seconds=0.5, plan_worker=worker, heartbeat=heartbeat)

    assert calls == 4
    assert heartbeats == 4
    assert len(delays) == 2 and all(delay > 0 for delay in delays)
    assert "error_code=ConnectionError" in caplog.text
    assert "database is restarting" not in caplog.text


@pytest.mark.asyncio
async def test_worker_once_mode_still_surfaces_iteration_failures(monkeypatch):
    worker = cast(PlanWorker, object())
    stub_branch_cleanup(monkeypatch)

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        raise ConnectionError("boom")

    monkeypatch.setattr(automation_worker, "process_once", process_once)

    with pytest.raises(ConnectionError):
        await automation_worker._run_worker_loop(poll_seconds=0.1, once=True, plan_worker=worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error"),
    [
        ("merged", "processed", None),
        # D11 / BUG-53: unknown pull requests become terminal instead of retrying forever.
        ("unlinked", "ignored", "pull_request_unlinked"),
    ],
)
async def test_worker_reconciles_durable_webhook_outbox_delivery(monkeypatch, outcome, expected_status, expected_error):
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
    monkeypatch.setattr(automation_worker, "create_pull_request_lifecycle_reconciler", lambda _: lifecycle)

    assert await automation_worker._process_pending_webhook_deliveries(cast(PlanWorker, worker))
    assert outbox.delivery.status == expected_status
    assert outbox.delivery.error_code == expected_error
    assert outbox.delivery.processed_at is not None
    assert lifecycle.calls == 1
    assert not await automation_worker._process_pending_webhook_deliveries(cast(PlanWorker, worker))


def test_deprecated_queue_flag_accepts_only_plans(monkeypatch):
    runs: list[dict] = []

    async def run_worker(**kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(automation_worker, "run_worker", run_worker)
    runner = CliRunner()

    rejected = runner.invoke(automation_worker.app, ["--once", "--queue", "legacy"])
    accepted = runner.invoke(automation_worker.app, ["--once", "--queue", "plans"])

    assert rejected.exit_code == 2
    assert accepted.exit_code == 0
    assert [run["once"] for run in runs] == [True]


@pytest.mark.asyncio
async def test_plans_queue_safely_idles_when_provider_json_is_absent(monkeypatch):
    async def create_runtime():
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    assert not await automation_worker.process_once()


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
    task = asyncio.create_task(automation_worker.run_worker(poll_seconds=10, plan_worker=worker))

    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_pr_poller_failures_are_restarted_without_stopping_the_queue(monkeypatch, caplog):
    """D9: a failing PR poller is logged and restarted with backoff; the queue keeps running."""
    worker = cast(PlanWorker, object())
    poller_runs = 0
    processed = 0

    class Poller:
        async def run(self, _stop_event):
            nonlocal poller_runs
            poller_runs += 1
            raise RuntimeError("polling lease database unavailable")

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        nonlocal processed
        assert plan_worker is worker
        processed += 1
        if processed >= 50:
            raise _StopLoop()
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(automation_worker, "_create_pr_polling_runtime", lambda _worker: Poller())
    monkeypatch.setattr(automation_worker, "process_once", process_once)
    monkeypatch.setattr(automation_worker, "backoff_delay", lambda *args, **kwargs: 0.0)

    with caplog.at_level("ERROR", logger="greenkube.cli.automation_worker"):
        with pytest.raises(_StopLoop):
            await automation_worker.run_worker(poll_seconds=10, plan_worker=worker)

    assert processed == 50
    assert poller_runs >= 2
    assert "pull-request polling runtime stopped" in caplog.text
    assert "error_code=RuntimeError" in caplog.text


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
