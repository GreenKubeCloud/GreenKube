"""Authenticated control-loop metrics listener for the controller process (A12 subset).

The controller runs the scheduled recommendation scans (E1), verification and
collection, so the control-loop metrics they record (``record_run``,
``record_analyzer_failure``, ``record_recommendation_scan_resources``,
``record_verification`` ...) live in the controller's process-local
registry. The API process only serves its own registry, so the controller
serves this one on ``GREENKUBE_CONTROLLER_METRICS_PORT`` at
``/prometheus/metrics``, with the same bearer authentication as the API and
the automation worker.

The listener is started for every controller replica, leader or standby:
counters stay correct under ``sum()`` because only the Lease holder scans.
"""

from __future__ import annotations

import asyncio
import logging
import os

from greenkube.cli.worker_metrics import EmbeddedMetricsServer, create_worker_metrics_server

logger = logging.getLogger(__name__)

CONTROLLER_METRICS_PORT_ENV = "GREENKUBE_CONTROLLER_METRICS_PORT"


def parse_controller_metrics_port(value: object | None = None) -> int | None:
    """Read the optional controller scrape port; ``None`` (unset or blank) disables the listener."""
    raw = os.environ.get(CONTROLLER_METRICS_PORT_ENV) if value is None else value
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str) or not raw.isascii() or not raw.strip().isdecimal():
        raise ValueError(f"{CONTROLLER_METRICS_PORT_ENV} must be an integer between 1 and 65535.")
    port = int(raw.strip())
    if not 1 <= port <= 65_535:
        raise ValueError(f"{CONTROLLER_METRICS_PORT_ENV} must be an integer between 1 and 65535.")
    return port


class ControllerMetricsListener:
    """A running embedded metrics server and the task serving it."""

    def __init__(self, server: EmbeddedMetricsServer, task: asyncio.Task[None], port: int) -> None:
        self.server = server
        self.task = task
        self.port = port

    async def stop(self) -> None:
        """Stop serving and wait for the listener to close."""
        self.server.should_exit = True
        await asyncio.gather(self.task, return_exceptions=True)


def _report_unexpected_exit(server: EmbeddedMetricsServer):
    def callback(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Controller metrics listener stopped unexpectedly: %s", error)
        elif not server.should_exit:
            logger.error("Controller metrics listener stopped unexpectedly; scrapes will fail until restart.")

    return callback


async def start_controller_metrics_listener(port: int | None = None) -> ControllerMetricsListener | None:
    """Start the listener when a port is configured and wait until it accepts scrapes.

    Returns ``None`` when ``GREENKUBE_CONTROLLER_METRICS_PORT`` is unset. A
    listener that cannot start (for example, the port is taken) fails the
    controller start so the misconfiguration is visible.
    """
    if port is None:
        port = parse_controller_metrics_port()
    if port is None:
        return None
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("The controller metrics port must be an integer between 1 and 65535.")

    server = create_worker_metrics_server(port)
    task = asyncio.create_task(server.serve(), name="controller-metrics")
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("Controller metrics listener exited before becoming ready.")
        await asyncio.sleep(0.01)
    task.add_done_callback(_report_unexpected_exit(server))
    logger.info("Serving controller metrics on port %d at /prometheus/metrics.", port)
    return ControllerMetricsListener(server, task, port)
