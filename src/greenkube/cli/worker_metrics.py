"""Authenticated metrics endpoint for a dedicated automation-worker process."""

from __future__ import annotations

import uvicorn
from fastapi import Depends, FastAPI, Request, Response

from greenkube.auth.scrape import verify_scrape_request
from greenkube.core.observability import generate_latest_observability


def verify_worker_scrape(request: Request) -> None:
    """The scrape token, or the bootstrap key unless disabled; a production worker without either fails closed."""
    verify_scrape_request(request, require_configured=True)


def create_worker_metrics_app() -> FastAPI:
    """Expose only this process's control-loop registry, behind the scrape token."""
    app = FastAPI(
        title="GreenKube automation worker metrics",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(verify_worker_scrape)],
    )

    @app.get("/prometheus/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(
            content=generate_latest_observability(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


class EmbeddedMetricsServer(uvicorn.Server):
    """Run Uvicorn in the worker loop without replacing the process signal handlers."""

    def install_signal_handlers(self) -> None:
        return


def create_worker_metrics_server(port: int) -> EmbeddedMetricsServer:
    """Create the in-process listener used by the dedicated worker entry point."""
    config = uvicorn.Config(
        create_worker_metrics_app(),
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return EmbeddedMetricsServer(config)
