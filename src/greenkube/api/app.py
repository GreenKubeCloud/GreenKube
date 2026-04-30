# src/greenkube/api/app.py
"""
FastAPI application factory for the GreenKube API.

Uses the factory pattern so the app can be created with or without
lifespan management (e.g., tests skip DB initialization).
The API also serves the SvelteKit SPA frontend when the build
directory is present in the image.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from greenkube import __version__
from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    verify_api_key,
)
from greenkube.api.metrics_endpoint import get_metrics_output, refresh_metrics_from_db
from greenkube.api.routers import config as config_router
from greenkube.api.routers import dashboard as dashboard_router
from greenkube.api.routers import health as health_router
from greenkube.api.routers import metrics, namespaces, nodes, recommendations, report
from greenkube.core.config import get_config
from greenkube.core.factory import get_savings_ledger_repository, get_summary_repository

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path("/app/frontend")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every HTTP response.

    These headers mitigate common web attacks (clickjacking, MIME-sniffing,
    XSS via content-type confusion, etc.) and are recommended by OWASP.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cache-Control"] = "no-store"
        # Content-Security-Policy: restrict resource loading for the SPA.
        # 'unsafe-inline' is required for SvelteKit's inline bootstrap script
        # and style injection. Without it the page is blank because the browser
        # blocks the <script> tag that bootstraps the SPA.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    logger.info("🚀 Starting GreenKube API...")
    from greenkube.core.db import get_db_manager

    await get_db_manager().connect()
    logger.info("✅ Database connection established.")
    yield
    logger.info("🛑 Shutting down GreenKube API...")
    await get_db_manager().close()
    logger.info("Database connection closed.")


def create_app(use_lifespan: bool = False) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        use_lifespan: If True, attach the lifespan handler that manages
                      database connections. Set to False for testing.

    Returns:
        A configured FastAPI application instance.
    """
    app = FastAPI(
        title="GreenKube API",
        description="FinGreenOps platform API — measure, report, and optimize carbon emissions and cloud costs.",
        version=__version__,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan if use_lifespan else None,
        dependencies=[Depends(verify_api_key)],
    )

    # --- Rate limiting ---
    # Configurable via API_RATE_LIMIT env var (default "60/minute").
    cfg = get_config()
    rate_limit = getattr(cfg, "API_RATE_LIMIT", "60/minute") or "60/minute"
    limiter = Limiter(key_func=get_remote_address, default_limits=[rate_limit])
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {exc.detail}"},
        )

    # CORS — configurable via CORS_ORIGINS env var (comma-separated).
    # Defaults to ["*"] for in-cluster use where the SPA is served from
    # the same origin. Override when the API is exposed via an ingress.
    cors_origins_str = cfg.CORS_ORIGINS if hasattr(cfg, "CORS_ORIGINS") else "*"
    cors_origins = [o.strip() for o in cors_origins_str.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Security headers (OWASP recommended)
    app.add_middleware(SecurityHeadersMiddleware)

    # Register API routers
    app.include_router(metrics.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(dashboard_router.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(namespaces.router, prefix="/api/v1", tags=["Namespaces"])
    app.include_router(nodes.router, prefix="/api/v1", tags=["Nodes"])
    app.include_router(recommendations.router, prefix="/api/v1", tags=["Recommendations"])
    app.include_router(config_router.router, prefix="/api/v1", tags=["Config"])
    app.include_router(health_router.router, prefix="/api/v1", tags=["Health"])
    app.include_router(report.router, prefix="/api/v1", tags=["Report"])

    # Prometheus metrics endpoint for Grafana dashboards
    # Exposed at /prometheus/metrics to avoid collision with the SPA /metrics route.
    @app.get("/prometheus/metrics", include_in_schema=False)
    async def prometheus_metrics(
        combined_repo=Depends(get_combined_metrics_repository),
        node_repo=Depends(get_node_repository),
        reco_repo=Depends(get_recommendation_repository),
        summary_repo=Depends(get_summary_repository),
    ):
        """Expose Prometheus-compatible metrics for scraping."""
        # The scheduler writes data to the DB in a separate container/process,
        await refresh_metrics_from_db(
            combined_repo,
            node_repo,
            reco_repo,
            savings_repo=get_savings_ledger_repository(),
            summary_repo=summary_repo,
        )
        return Response(
            content=get_metrics_output(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # Serve the SPA frontend if the build directory exists
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Mount the SvelteKit SPA static files and add a catch-all fallback.

    The SPA is built with adapter-static and produces an index.html
    plus hashed assets in _app/. FastAPI serves these directly and
    falls back to index.html for client-side routing.
    """
    if not FRONTEND_DIR.is_dir():
        logger.info("Frontend directory not found at %s — SPA disabled.", FRONTEND_DIR)
        return

    index_html = FRONTEND_DIR / "index.html"
    if not index_html.is_file():
        logger.warning("Frontend directory exists but index.html is missing — SPA disabled.")
        return

    logger.info("Serving SPA frontend from %s", FRONTEND_DIR)

    # Mount immutable hashed assets with aggressive caching
    app_assets = FRONTEND_DIR / "_app"
    if app_assets.is_dir():
        app.mount("/_app", StaticFiles(directory=str(app_assets)), name="frontend-app")

    # Serve other static files (favicon, etc.)
    app.mount("/static-frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend-root")

    # SPA catch-all: any route not matched by /api/* returns index.html
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Serve the SPA index.html for all non-API routes."""
        file_path = FRONTEND_DIR / full_path
        if file_path.is_file() and not full_path.startswith("api/"):
            return FileResponse(str(file_path))
        return FileResponse(str(index_html))


def main():
    """Entry point for the greenkube-api console script."""
    cfg = get_config()
    logging.basicConfig(
        level=cfg.LOG_LEVEL.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    app = create_app(use_lifespan=True)
    uvicorn.run(app, host=cfg.API_HOST, port=cfg.API_PORT)
