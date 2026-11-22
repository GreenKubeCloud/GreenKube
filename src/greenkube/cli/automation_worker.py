"""CLI entry point for the optional dedicated plan executor (roadmap F8).

The worker only claims and executes queued plan operations; operation leases make
it safe next to the controller's own executor and next to other worker replicas.
It follows the same delivery gate as the controller's executor (F12): nothing is
claimed while ``automation.policy.pauseDelivery`` is set or while the readiness
reconciler does not report delivery as active.
The webhook outbox, pull-request polling, branch cleanup, plan expiry and retention
run in the controller's automation loop (:mod:`greenkube.automation.controller`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import typer

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError, create_plan_runtime
from greenkube.automation.plan_worker import PlanWorker
from greenkube.automation.supervision import backoff_delay, error_code
from greenkube.cli.worker_metrics import EmbeddedMetricsServer, create_worker_metrics_server
from greenkube.core.observability import (
    record_automation_failure,
    record_automation_worker_heartbeat,
    set_automation_worker_up,
)

logger = logging.getLogger(__name__)
app = typer.Typer(name="automation-worker", help="Process queued GreenKube GitOps plan operations.")
WORKER_METRICS_PORT_ENV = "GREENKUBE_WORKER_METRICS_PORT"
WORKER_ITERATION_MAX_BACKOFF_SECONDS = 60.0
#: A delivery capability row older than this is ignored (the controller stopped reporting).
DELIVERY_GATE_STALE_AFTER = timedelta(minutes=5)


async def delivery_allowed(now: datetime | None = None) -> bool:
    """True when queued operations may open pull requests; fails closed.

    The same gate as the controller's executor: ``pauseDelivery`` is off and the
    readiness reconciler's global ``delivery`` capability is active and fresh.
    """
    from greenkube.core.config import get_config

    if get_config().automation_delivery_paused:
        return False
    try:
        from greenkube.core.db import get_db_manager
        from greenkube.storage.automation_capability_repository import (
            SCOPE_GLOBAL,
            AutomationCapabilityRepository,
        )

        rows = await AutomationCapabilityRepository(get_db_manager()).list_capabilities(SCOPE_GLOBAL)
    except Exception as exc:
        logger.warning("Delivery gate unreadable; no operation is claimed. error_type=%s", type(exc).__name__)
        return False
    row = next((row for row in rows if row.subject == "delivery"), None)
    if row is None or row.state != "active" or row.updated_at is None:
        return False
    return (now or datetime.now(timezone.utc)) - row.updated_at <= DELIVERY_GATE_STALE_AFTER


async def process_once(
    worker_id: str | None = None,
    *,
    plan_worker: PlanWorker | None = None,
) -> bool:
    if plan_worker is None:
        try:
            plan_worker = (await create_plan_runtime(allow_empty=True, reconcile_providers=False)).worker
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
    plan_worker: PlanWorker | None = None,
    metrics_port: int | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    if metrics_port is None:
        await _run_worker_loop(poll_seconds=poll_seconds, once=once, plan_worker=plan_worker, heartbeat=heartbeat)
        return
    if type(metrics_port) is not int or not 1 <= metrics_port <= 65_535:
        raise ValueError("metrics_port must be an integer between 1 and 65535.")

    metrics_server = create_worker_metrics_server(metrics_port)
    metrics_task = asyncio.create_task(metrics_server.serve(), name="automation-worker-metrics")
    worker_task: asyncio.Task[None] | None = None
    try:
        await _wait_for_worker_metrics_server(metrics_server, metrics_task)
        worker_task = asyncio.create_task(
            _run_worker_loop(poll_seconds=poll_seconds, once=once, plan_worker=plan_worker, heartbeat=heartbeat),
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
    heartbeat: Callable[[], None] | None = None,
    delivery_gate: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    beat = heartbeat or _no_heartbeat
    gate = delivery_gate or delivery_allowed
    if plan_worker is None:
        try:
            plan_worker = (await create_plan_runtime(allow_empty=True, reconcile_providers=False)).worker
        except PlanProvidersNotConfiguredError as exc:
            logger.warning("Plan queue is idle: %s", exc)
            if once:
                return
            while True:
                beat()
                await asyncio.sleep(poll_seconds)
    if plan_worker is None:
        return

    worker_id = str(uuid.uuid4())
    consecutive_failures = 0
    gated: bool | None = None
    try:
        while True:
            record_automation_worker_heartbeat(datetime.now(timezone.utc).timestamp())
            beat()
            # pauseDelivery and dormant delivery hold every queued operation (F12).
            try:
                allowed = bool(await gate())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Delivery gate failed; no operation is claimed. error_type=%s", type(exc).__name__)
                allowed = False
            if gated is None or gated is allowed:  # first iteration or the gate changed
                gated = not allowed
                set_automation_worker_up(allowed)
                if gated:
                    logger.info("Pull-request delivery is paused or dormant; queued operations wait.")
            if not allowed:
                if once:
                    return
                await asyncio.sleep(poll_seconds)
                continue
            try:
                processed = await process_once(worker_id=worker_id, plan_worker=plan_worker)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if once:
                    raise
                # One failed iteration (database hiccup, provider outage, bug in one operation)
                # must never end the queue loop (D9, BUG-51).
                consecutive_failures += 1
                delay = backoff_delay(
                    consecutive_failures,
                    initial=min(max(poll_seconds, 0.1), WORKER_ITERATION_MAX_BACKOFF_SECONDS),
                    maximum=WORKER_ITERATION_MAX_BACKOFF_SECONDS,
                )
                logger.error(
                    "Automation worker iteration failed; retrying in %.1fs. "
                    "error_code=%s error_type=%s consecutive_failures=%d",
                    delay,
                    error_code(exc),
                    type(exc).__name__,
                    consecutive_failures,
                )
                record_automation_failure("worker")
                await asyncio.sleep(delay)
                continue
            consecutive_failures = 0
            if once:
                return
            if not processed:
                await asyncio.sleep(poll_seconds)
    finally:
        set_automation_worker_up(False)


def _no_heartbeat() -> None:
    return None


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Process at most one plan operation."),
    poll_seconds: float = typer.Option(5.0, min=0.1, help="Delay when the queue is empty."),
    queue: str = typer.Option("plans", "--queue", hidden=True, help="Deprecated; 'plans' is the only queue."),
):
    if queue != "plans":
        raise typer.BadParameter("the only queue is 'plans'.", param_hint="--queue")
    asyncio.run(run_worker(poll_seconds=poll_seconds, once=once, metrics_port=parse_worker_metrics_port()))


if __name__ == "__main__":
    app()
