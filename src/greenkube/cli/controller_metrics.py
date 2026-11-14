"""Authenticated Prometheus endpoint for the controller process (roadmap A12, BUG-14).

The controller runs collection, compression, verification, GitOps sync
observation and, since E1, the scheduled recommendation scans. Their telemetry
(``record_verification``, ``record_run``, ``record_analyzer_failure``,
``record_recommendation_scan_resources`` ...) lives in the process-local
control-loop registry (:mod:`greenkube.core.observability`), which the API
process cannot see. This module serves that registry on
``CONTROLLER_METRICS_PORT`` (default 9102) at ``/prometheus/metrics`` (the
chart-wide convention) and at Prometheus' default ``/metrics`` path (the
controller serves no UI, so nothing collides with it).

The listener is started for every controller replica, leader or standby:
counters stay correct under ``sum()`` because only the Lease holder scans.

Access model: the Helm chart keeps the admin API key out of the controller
(least privilege) and gives it the dedicated scrape token
``GREENKUBE_METRICS_TOKEN`` instead, which the controller ServiceMonitor sends
as a bearer token (:mod:`greenkube.auth.scrape`). The endpoint is reachable on
the pod network only (ClusterIP Service, no Ingress; restrict it to Prometheus
with ``monitoring.networkPolicy``). Without any configured credential (an
install whose ``secrets.existingSecret`` holds no scrape token) it stays open,
as before the scrape token existed: it serves bounded, non-sensitive
operational counters.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from greenkube.auth.scrape import verify_scrape_request
from greenkube.cli.worker_metrics import EmbeddedMetricsServer
from greenkube.core.observability import generate_latest_observability

logger = logging.getLogger(__name__)

CONTROLLER_METRICS_PATH = "/prometheus/metrics"
#: Prometheus' default scrape path, served as an alias of ``CONTROLLER_METRICS_PATH``.
CONTROLLER_METRICS_DEFAULT_PATH = "/metrics"
CONTROLLER_LIVENESS_PATH = "/healthz"
CONTROLLER_READINESS_PATH = "/readyz"
CONTROLLER_LOOPS_PATH = "/loops"
STARTUP_TIMEOUT_SECONDS = 10.0


def verify_scrape_token(request: Request) -> None:
    """Require the scrape token (or the bootstrap key, unless disabled) when the controller has one."""
    verify_scrape_request(request, require_configured=False)


class LoopHealth(Protocol):
    """Health view of the controller's loop registry (:class:`greenkube.core.loops.LoopRegistry`)."""

    def ready(self) -> bool: ...

    def snapshot(self) -> dict[str, Any]: ...


def create_controller_metrics_app(health: LoopHealth | None = None) -> FastAPI:
    """Expose this process's control-loop registry and the health of its loops (M8).

    ``/healthz`` (process alive) and ``/readyz`` (every registered loop alive with a
    fresh heartbeat; 503 otherwise) are unauthenticated probe endpoints that reveal
    nothing but a verdict. ``/loops`` returns the per-loop snapshot (state, last
    success, duration, consecutive failures, last error code) and, like the metrics,
    requires the scrape token when the controller has one.
    """
    app = FastAPI(
        title="GreenKube controller metrics",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(CONTROLLER_METRICS_PATH, include_in_schema=False, dependencies=[Depends(verify_scrape_token)])
    @app.get(CONTROLLER_METRICS_DEFAULT_PATH, include_in_schema=False, dependencies=[Depends(verify_scrape_token)])
    async def prometheus_metrics() -> Response:
        return Response(
            content=generate_latest_observability(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get(CONTROLLER_LIVENESS_PATH, include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(CONTROLLER_READINESS_PATH, include_in_schema=False)
    async def readiness() -> JSONResponse:
        ready = True if health is None else bool(health.ready())
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready})

    @app.get(CONTROLLER_LOOPS_PATH, include_in_schema=False, dependencies=[Depends(verify_scrape_token)])
    async def loops() -> dict[str, Any]:
        if health is None:
            return {"ready": True, "loops": []}
        return health.snapshot()

    return app


def create_controller_metrics_server(
    port: int,
    host: str = "0.0.0.0",
    *,
    health: LoopHealth | None = None,
) -> EmbeddedMetricsServer:
    """Create the in-process listener; it never replaces the controller's signal handlers."""
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("CONTROLLER_METRICS_PORT must be an integer between 1 and 65535.")
    config = uvicorn.Config(
        create_controller_metrics_app(health),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    return EmbeddedMetricsServer(config)


async def _serve(server: EmbeddedMetricsServer) -> None:
    """Run the listener; Uvicorn's ``sys.exit`` on bind errors must not stop the controller."""
    try:
        await server.serve()
    except SystemExit as exc:
        raise RuntimeError(f"controller metrics listener exited with status {exc.code}") from None


def _report_unexpected_exit(server: EmbeddedMetricsServer):
    """Log a listener that stops while the controller keeps running."""

    def callback(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Controller metrics listener stopped unexpectedly: %s", error)
        elif not server.should_exit:
            logger.error("Controller metrics listener stopped unexpectedly; scrapes will fail until restart.")

    return callback


@dataclass
class ControllerMetricsHandle:
    server: EmbeddedMetricsServer
    task: asyncio.Task[None]


async def start_controller_metrics_server(
    port: int,
    host: str = "0.0.0.0",
    *,
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    health: LoopHealth | None = None,
) -> ControllerMetricsHandle | None:
    """Start the listener when ``port`` is set; return ``None`` when disabled or unavailable.

    Telemetry must never stop collection: a listener that cannot bind is
    logged and skipped instead of failing the controller.
    """
    if not port:
        logger.info("Controller metrics endpoint disabled (CONTROLLER_METRICS_PORT=0).")
        return None
    server = create_controller_metrics_server(port, host, health=health)
    task = asyncio.create_task(_serve(server), name="controller-metrics")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + startup_timeout
    while not server.started:
        if task.done() or loop.time() >= deadline:
            break
        await asyncio.sleep(0.01)
    if not server.started:
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        logger.error("Controller metrics endpoint could not start on port %s; continuing without it.", port)
        return None
    task.add_done_callback(_report_unexpected_exit(server))
    logger.info("Controller metrics endpoint listening on %s:%s%s", host, port, CONTROLLER_METRICS_PATH)
    return ControllerMetricsHandle(server=server, task=task)


async def stop_controller_metrics_server(handle: ControllerMetricsHandle | None) -> None:
    """Stop the listener started by :func:`start_controller_metrics_server`."""
    if handle is None:
        return
    handle.server.should_exit = True
    await asyncio.gather(handle.task, return_exceptions=True)
