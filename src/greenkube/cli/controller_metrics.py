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

Access model: the Helm chart keeps the API key out of the controller (least
privilege), so the endpoint is reachable on the pod network only (ClusterIP
Service, no Ingress; restrict it to Prometheus with
``monitoring.networkPolicy``). It serves bounded, non-sensitive operational
counters. When an API key *is* configured in the controller's environment, a
matching bearer token is required.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response

from greenkube.cli.worker_metrics import EmbeddedMetricsServer
from greenkube.core.observability import generate_latest_observability

logger = logging.getLogger(__name__)

CONTROLLER_METRICS_PATH = "/prometheus/metrics"
#: Prometheus' default scrape path, served as an alias of ``CONTROLLER_METRICS_PATH``.
CONTROLLER_METRICS_DEFAULT_PATH = "/metrics"
STARTUP_TIMEOUT_SECONDS = 10.0


def verify_scrape_token(request: Request) -> None:
    """Require the API key as a bearer token only when the controller has one."""
    from greenkube.core.config import get_config

    api_key = get_config().API_KEY
    if not api_key:
        return
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token, api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_controller_metrics_app() -> FastAPI:
    """Expose only this process's control-loop registry."""
    app = FastAPI(
        title="GreenKube controller metrics",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_scrape_token)],
    )

    @app.get(CONTROLLER_METRICS_PATH, include_in_schema=False)
    @app.get(CONTROLLER_METRICS_DEFAULT_PATH, include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(
            content=generate_latest_observability(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


def create_controller_metrics_server(port: int, host: str = "0.0.0.0") -> EmbeddedMetricsServer:
    """Create the in-process listener; it never replaces the controller's signal handlers."""
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("CONTROLLER_METRICS_PORT must be an integer between 1 and 65535.")
    config = uvicorn.Config(
        create_controller_metrics_app(),
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
) -> ControllerMetricsHandle | None:
    """Start the listener when ``port`` is set; return ``None`` when disabled or unavailable.

    Telemetry must never stop collection: a listener that cannot bind is
    logged and skipped instead of failing the controller.
    """
    if not port:
        logger.info("Controller metrics endpoint disabled (CONTROLLER_METRICS_PORT=0).")
        return None
    server = create_controller_metrics_server(port, host)
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
