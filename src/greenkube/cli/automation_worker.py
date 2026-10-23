"""CLI entry point for processing durable GitOps plan operations."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

import typer

from greenkube.automation.branch_cleanup_runtime import BranchCleanupRuntime
from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError, create_plan_runtime
from greenkube.automation.plan_worker import PlanWorker
from greenkube.automation.pr_lifecycle_runtime import create_pull_request_lifecycle_reconciler
from greenkube.automation.pr_polling_runtime import PollablePullRequestRepository, PullRequestPollingRuntime
from greenkube.cli.worker_metrics import EmbeddedMetricsServer, create_worker_metrics_server
from greenkube.core.observability import (
    record_automation_failure,
    record_automation_worker_heartbeat,
    set_automation_worker_up,
)
from greenkube.storage.base_gitops_repository import PullRequestPollingLeaseRepository, WebhookDelivery
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

logger = logging.getLogger(__name__)
app = typer.Typer(name="automation-worker", help="Process queued GreenKube GitOps plan operations.")
WORKER_METRICS_PORT_ENV = "GREENKUBE_WORKER_METRICS_PORT"
WORKER_HEARTBEAT_RETENTION_DAYS = 7
WEBHOOK_OUTBOX_BATCH_SIZE = 10
WEBHOOK_PROCESSING_LEASE_SECONDS = 300
PENDING_PULL_REQUEST_RECOVERY_GRACE_SECONDS = 900


class _WorkerMaintenanceRepository(Protocol):
    async def prune_worker_heartbeats(self, *, cutoff: datetime) -> int: ...


class _PendingPullRequestRecoveryRepository(Protocol):
    async def reconcile_open_pull_requests(self, *, stale_before: datetime, limit: int) -> int: ...


class _WebhookOutboxRepository(Protocol):
    async def list_pending_webhook_deliveries(
        self,
        *,
        limit: int,
        stale_before: datetime,
    ) -> list[WebhookDelivery]: ...

    async def claim_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        stale_before: datetime,
    ) -> bool: ...

    async def update_webhook_delivery(
        self,
        provider_id: str,
        delivery_id: str,
        *,
        status: str,
        processed_at: datetime | None = None,
        error_code: str | None = None,
        expected_statuses: Sequence[str] | None = None,
    ) -> WebhookDelivery | None: ...


async def process_once(
    worker_id: str | None = None,
    *,
    queue: str = "plans",
    plan_worker: PlanWorker | None = None,
) -> bool:
    if queue != "plans":
        raise ValueError("queue must be 'plans'; annotation-based operations are no longer supported.")
    if plan_worker is None:
        try:
            plan_worker = (await create_plan_runtime()).worker
        except PlanProvidersNotConfiguredError as exc:
            logger.warning("Plan queue is idle: %s", exc)
            return False
    return await plan_worker.process_once(worker_id or str(uuid.uuid4()))


def parse_worker_metrics_port(value: object | None = None) -> int | None:
    """Read the optional dedicated-worker scrape port from an explicit value or environment."""
    raw = os.environ.get(WORKER_METRICS_PORT_ENV) if value is None else value
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{WORKER_METRICS_PORT_ENV} must be an integer between 1 and 65535.")
    port = int(raw)
    if not 1 <= port <= 65_535:
        raise ValueError(f"{WORKER_METRICS_PORT_ENV} must be an integer between 1 and 65535.")
    return port


async def run_worker(
    poll_seconds: float = 5.0,
    once: bool = False,
    *,
    queue: str = "plans",
    plan_worker: PlanWorker | None = None,
    metrics_port: int | None = None,
) -> None:
    if queue != "plans":
        raise ValueError("queue must be 'plans'; annotation-based operations are no longer supported.")
    if metrics_port is None:
        await _run_worker_loop(poll_seconds=poll_seconds, once=once, plan_worker=plan_worker)
        return
    if type(metrics_port) is not int or not 1 <= metrics_port <= 65_535:
        raise ValueError("metrics_port must be an integer between 1 and 65535.")

    metrics_server = create_worker_metrics_server(metrics_port)
    metrics_task = asyncio.create_task(metrics_server.serve(), name="automation-worker-metrics")
    worker_task: asyncio.Task[None] | None = None
    try:
        await _wait_for_worker_metrics_server(metrics_server, metrics_task)
        worker_task = asyncio.create_task(
            _run_worker_loop(poll_seconds=poll_seconds, once=once, plan_worker=plan_worker),
            name="automation-worker-loop",
        )
        done, _ = await asyncio.wait(
            {worker_task, metrics_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if metrics_task in done:
            await metrics_task
            raise RuntimeError("Worker metrics server stopped unexpectedly.")
        await worker_task
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        metrics_server.should_exit = True
        await asyncio.gather(metrics_task, return_exceptions=True)


async def _wait_for_worker_metrics_server(
    server: EmbeddedMetricsServer,
    server_task: asyncio.Task[None],
) -> None:
    while not server.started:
        if server_task.done():
            await server_task
            raise RuntimeError("Worker metrics server exited before becoming ready.")
        await asyncio.sleep(0.01)
    if server_task.done():
        await server_task
        raise RuntimeError("Worker metrics server exited before the worker loop started.")


async def _run_worker_loop(
    poll_seconds: float = 5.0,
    once: bool = False,
    *,
    plan_worker: PlanWorker | None = None,
) -> None:
    if plan_worker is None:
        try:
            plan_worker = (await create_plan_runtime()).worker
        except PlanProvidersNotConfiguredError as exc:
            logger.warning("Plan queue is idle: %s", exc)
            if once:
                return
            while True:
                await asyncio.sleep(poll_seconds)
    if plan_worker is None:
        return

    worker_id = str(uuid.uuid4())
    worker_repository = getattr(plan_worker, "plans", None)
    if worker_repository is not None:
        await cast(_WorkerMaintenanceRepository, worker_repository).prune_worker_heartbeats(
            cutoff=datetime.now(timezone.utc) - timedelta(days=WORKER_HEARTBEAT_RETENTION_DAYS)
        )
    pull_request_repository = getattr(plan_worker, "pull_request_repository", None)
    if pull_request_repository is not None:
        quarantined = await cast(
            _PendingPullRequestRecoveryRepository,
            pull_request_repository,
        ).reconcile_open_pull_requests(
            stale_before=datetime.now(timezone.utc) - timedelta(seconds=PENDING_PULL_REQUEST_RECOVERY_GRACE_SECONDS),
            limit=MAX_PULL_REQUEST_POLL_BATCH_SIZE,
        )
        if quarantined:
            logger.warning(
                "Quarantined %d stale pending pull-request attempts without plan/provider/PR references.",
                quarantined,
            )

    poller_task: asyncio.Task[None] | None = None
    poller_stop_event = asyncio.Event()
    if not once:
        poller = _create_pr_polling_runtime(plan_worker)
        poller_task = asyncio.create_task(
            poller.run(poller_stop_event),
            name="gitops-pr-polling",
        )

    set_automation_worker_up(True)
    try:
        while True:
            record_automation_worker_heartbeat(datetime.now(timezone.utc).timestamp())
            if poller_task is not None and poller_task.done():
                await _ensure_pr_poller_running(poller_task)
            webhook_processed = await _process_pending_webhook_deliveries(plan_worker)
            processed = await process_once(worker_id=worker_id, plan_worker=plan_worker)
            if poller_task is not None and poller_task.done():
                await _ensure_pr_poller_running(poller_task)
            if once:
                await _create_branch_cleanup_runtime(plan_worker).cleanup_once()
                return
            if not processed and not webhook_processed:
                if poller_task is None:
                    await asyncio.sleep(poll_seconds)
                else:
                    try:
                        await asyncio.wait_for(asyncio.shield(poller_task), timeout=poll_seconds)
                    except TimeoutError:
                        pass
                    else:
                        await _ensure_pr_poller_running(poller_task)
    finally:
        set_automation_worker_up(False)
        if poller_task is not None:
            poller_stop_event.set()
            poller_task.cancel()
            poller_result = (await asyncio.gather(poller_task, return_exceptions=True))[0]
            if isinstance(poller_result, Exception):
                raise poller_result


async def _ensure_pr_poller_running(poller_task: asyncio.Task[None]) -> None:
    await poller_task
    raise RuntimeError("Git provider pull-request polling runtime stopped unexpectedly.")


def _create_pr_polling_runtime(plan_worker: PlanWorker) -> PullRequestPollingRuntime:
    return PullRequestPollingRuntime(
        cast(PollablePullRequestRepository, plan_worker.pull_request_repository),
        create_pull_request_lifecycle_reconciler(plan_worker),
        cast(PullRequestPollingLeaseRepository, plan_worker.plans),
        branch_cleanup=_create_branch_cleanup_runtime(plan_worker),
    )


def _create_branch_cleanup_runtime(plan_worker: PlanWorker) -> BranchCleanupRuntime:
    return BranchCleanupRuntime(
        pull_request_repository=cast(Any, plan_worker.pull_request_repository),
        plan_repository=cast(Any, plan_worker.plans),
        operation_repository=cast(Any, plan_worker.operations),
        provider_resolver=plan_worker.provider_resolver,
    )


async def _process_pending_webhook_deliveries(plan_worker: PlanWorker) -> bool:
    raw_repository = getattr(plan_worker, "plans", None)
    if raw_repository is None:
        return False
    repository = cast(_WebhookOutboxRepository, raw_repository)

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=WEBHOOK_PROCESSING_LEASE_SECONDS)
    deliveries = await repository.list_pending_webhook_deliveries(
        limit=WEBHOOK_OUTBOX_BATCH_SIZE,
        stale_before=stale_before,
    )
    if not deliveries:
        return False

    reconciler = create_pull_request_lifecycle_reconciler(plan_worker)
    worked = False
    for delivery in deliveries:
        event = delivery.event
        if event is None:
            provider_id = delivery.provider_id
            delivery_id = delivery.delivery_id
            if not isinstance(provider_id, str) or not isinstance(delivery_id, str):
                logger.error("Webhook outbox row %s has no valid provider or delivery ID.", delivery.id)
                record_automation_failure("webhook")
                continue
            if not await repository.claim_webhook_delivery(provider_id, delivery_id, stale_before=stale_before):
                continue
            worked = True
            await repository.update_webhook_delivery(
                provider_id,
                delivery_id,
                status="rejected",
                processed_at=now,
                error_code="invalid_persisted_event",
                expected_statuses=("processing",),
            )
            continue
        if not await repository.claim_webhook_delivery(
            event.provider_id,
            event.delivery_id,
            stale_before=stale_before,
        ):
            continue
        worked = True
        try:
            result = await reconciler.reconcile(event, newly_deduplicated=True)
        except Exception as error:
            await repository.update_webhook_delivery(
                event.provider_id,
                event.delivery_id,
                status="retryable",
                error_code="lifecycle_reconcile_failed",
                expected_statuses=("processing",),
            )
            logger.warning(
                "Webhook outbox reconciliation failed for delivery %s (%s).",
                event.delivery_id,
                type(error).__name__,
            )
            record_automation_failure("webhook")
            continue

        outcome = getattr(getattr(result, "outcome", None), "value", None)
        if outcome == "unlinked":
            status, error_code = "retryable", "pull_request_unlinked"
        elif outcome == "mismatched":
            status, error_code = "rejected", "pull_request_mismatched"
        elif isinstance(outcome, str):
            status, error_code = "processed", None
        else:
            status, error_code = "retryable", "invalid_lifecycle_outcome"
        await repository.update_webhook_delivery(
            event.provider_id,
            event.delivery_id,
            status=status,
            processed_at=now if status in {"processed", "rejected"} else None,
            error_code=error_code,
            expected_statuses=("processing",),
        )
    return worked


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Process at most one plan operation."),
    poll_seconds: float = typer.Option(5.0, min=0.1, help="Delay when the queue is empty."),
    queue: str = typer.Option("plans", "--queue", help="The supported queue is 'plans'."),
):
    asyncio.run(
        run_worker(
            poll_seconds=poll_seconds,
            once=once,
            queue=queue,
            metrics_port=parse_worker_metrics_port(),
        )
    )


if __name__ == "__main__":
    app()
