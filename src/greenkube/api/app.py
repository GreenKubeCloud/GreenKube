# src/greenkube/api/app.py
"""
FastAPI application factory for the GreenKube API.

Uses the factory pattern so the app can be created with or without
lifespan management (e.g., tests skip DB initialization).
The API also serves the SvelteKit SPA frontend when the build
directory is present in the image.
"""

import asyncio
import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from limits import parse as parse_rate_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from greenkube import __version__
from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    verify_api_key,
)
from greenkube.api.metrics_endpoint import get_metrics_output, refresh_metrics_from_db
from greenkube.api.routers import automation as automation_router
from greenkube.api.routers import config as config_router
from greenkube.api.routers import dashboard as dashboard_router
from greenkube.api.routers import health as health_router
from greenkube.api.routers import metrics, namespaces, nodes, recommendations, report, repository_bindings
from greenkube.api.startup import run_startup_recommendation_scan
from greenkube.core.config import get_config
from greenkube.core.factory import get_savings_ledger_repository, get_summary_repository

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path("/app/frontend")


def _parse_networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid trusted proxy network: %s", item)
    return tuple(networks)


def _is_trusted_ip(value: str, networks) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every HTTP response.

    These headers mitigate common web attacks (clickjacking, MIME-sniffing,
    XSS via content-type confusion, etc.) and are recommended by OWASP.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        is_spa_html = "text/html" in content_type and "/api/" not in request.url.path
        is_static_asset = request.url.path.startswith(("/_app/", "/static-frontend/"))
        is_api = "/api/" in request.url.path

        # Common security headers for all responses
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Document-only headers — only meaningful for HTML pages, not for
        # JSON API responses or static assets.  Setting them on every response
        # bloats the header payload and can produce malformed responses when
        # a reverse proxy (e.g. Caddy with forward_auth) merges headers from
        # both the auth subrequest and the backend response.
        if is_spa_html:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
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

        # Caching: no-store on the SPA entry point ensures users always get
        # the latest build.  API responses get no-cache (not no-store) to
        # avoid interfering with reverse-proxy session cookies (e.g. Authentik
        # forward-auth).  Hashed immutable static assets can be cached
        # aggressively.
        if is_spa_html:
            response.headers["Cache-Control"] = "no-store"
        elif is_static_asset:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif is_api:
            response.headers["Cache-Control"] = "no-cache"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client, per-route sliding-window rate limiting for API routes.

    Enforcement is done with the ``limits`` library directly: slowapi's
    middleware cannot discover endpoints registered through FastAPI's
    ``_IncludedRouter`` (FastAPI >= 0.121), which made the previous
    configuration inert. Static SPA assets are not rate limited so a normal
    page load cannot exhaust the budget.
    """

    _LIMITED_PREFIXES = ("/api/",)
    _LIMITED_EXACT = ("/prometheus/metrics",)

    def __init__(self, app, rate_limit: str, storage: MemoryStorage | None = None, limiter=None):
        super().__init__(app)
        self._limits = [parse_rate_limit(part.strip()) for part in rate_limit.split(";") if part.strip()]
        self._storage = storage or MemoryStorage()
        self._limiter = limiter or MovingWindowRateLimiter(self._storage)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not (path.startswith(self._LIMITED_PREFIXES) or path in self._LIMITED_EXACT):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        trusted_proxies = _parse_networks(getattr(get_config(), "TRUSTED_PROXY_IPS", ""))
        if _is_trusted_ip(client, trusted_proxies):
            forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
            if forwarded:
                client = forwarded
        identifiers = (client, path)
        for limit in self._limits:
            if not self._limiter.hit(limit, *identifiers):
                stats = self._limiter.get_window_stats(limit, *identifiers)
                retry_after = max(1, int(stats.reset_time - time.time()))
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded: {limit}"},
                    headers={"Retry-After": str(retry_after)},
                )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    logger.info("🚀 Starting GreenKube API...")
    from greenkube.core.db import get_db_manager

    await get_db_manager().connect()
    logger.info("✅ Database connection established.")

    # Start tracked background startup tasks so we can cancel them on shutdown.
    app.state._startup_tasks = []
    app.state._startup_tasks.append(asyncio.create_task(run_startup_recommendation_scan()))
    app.state.ready = True

    yield
    logger.info("🛑 Shutting down GreenKube API...")
    app.state.ready = False
    # Cancel any tracked startup/background tasks
    try:
        for t in getattr(app.state, "_startup_tasks", []) or []:
            if not t.done():
                t.cancel()
        # Wait a short time for tasks to cancel
        await asyncio.gather(*(t for t in getattr(app.state, "_startup_tasks", []) or []), return_exceptions=True)
    except Exception:
        logger.exception("Error while cancelling startup tasks")

    await get_db_manager().close()
    logger.info("Database connection closed.")
    # Close shared Kubernetes ApiClient (if created during runtime)
    try:
        from greenkube.core.k8s_client import close_k8s_client

        await close_k8s_client()
        logger.info("Kubernetes ApiClient closed.")
    except Exception:
        logger.exception("Failed to close Kubernetes ApiClient during shutdown.")


def create_app(use_lifespan: bool = False) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        use_lifespan: If True, attach the lifespan handler that manages
                      database connections. Set to False for testing.

    Returns:
        A configured FastAPI application instance.
    """
    cfg = get_config()
    root_path = getattr(cfg, "ROOT_PATH", "") or ""

    app = FastAPI(
        title="GreenKube API",
        description="FinGreenOps platform API — measure, report, and optimize carbon emissions and cloud costs.",
        version=__version__,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan if use_lifespan else None,
        dependencies=[Depends(verify_api_key)],
        root_path=root_path,
    )

    # --- Rate limiting ---
    # Configurable via API_RATE_LIMIT env var (default "60/minute").
    # Multiple limits can be combined with ';' (e.g. "60/minute;1000/hour").
    rate_limit = getattr(cfg, "API_RATE_LIMIT", "60/minute") or "60/minute"
    app.add_middleware(RateLimitMiddleware, rate_limit=rate_limit, storage=MemoryStorage())

    # CORS is closed by default. Explicit origins are required for cross-origin
    # browser clients; same-origin SPA requests do not need CORS headers.
    cors_origins = [o.strip() for o in (getattr(cfg, "CORS_ORIGINS", "") or "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    trusted_hosts = [h.strip() for h in getattr(cfg, "TRUSTED_HOSTS", "*").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts or ["*"])

    # Security headers (OWASP recommended)
    app.add_middleware(SecurityHeadersMiddleware)

    # Register API routers
    app.include_router(metrics.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(dashboard_router.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(namespaces.router, prefix="/api/v1", tags=["Namespaces"])
    app.include_router(nodes.router, prefix="/api/v1", tags=["Nodes"])
    app.include_router(recommendations.router, prefix="/api/v1", tags=["Recommendations"])
    app.include_router(automation_router.router, prefix="/api/v1", tags=["Automation"])
    app.include_router(config_router.router, prefix="/api/v1", tags=["Config"])
    app.include_router(health_router.router, prefix="/api/v1", tags=["Health"])
    app.include_router(report.router, prefix="/api/v1", tags=["Report"])
    app.include_router(repository_bindings.router, prefix="/api/v1", tags=["Repository bindings"])

    @app.get("/api/v1/health/heartbeat", include_in_schema=False)
    async def heartbeat():
        """Return a lightweight liveness response without probing dependencies."""
        return {
            "status": "ok",
            "ready": bool(getattr(app.state, "ready", True)),
            "timestamp": datetime.now(timezone.utc),
        }

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

    # SPA catch-all: any route not matched by /api/* returns index.html.
    # Excludes well-known proxy/ingress paths so they return 404 instead
    # of serving the SPA when the reverse proxy misroutes them.
    _PROXY_PATHS = (
        "/outpost.goauthentik.io",
        "/.well-known/",
        "/oauth2/",
        "/oidc/",
        "/sso/",
        "/auth/",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Serve the SPA index.html for all non-API routes."""
        # API paths must never fall back to the SPA: unknown API routes
        # return a JSON 404 instead of a 200 HTML page.
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # Well-known proxy/ingress paths return 404 instead of the SPA when
        # the reverse proxy misroutes them.
        if any(("/" + full_path).startswith(p) for p in _PROXY_PATHS):
            return Response(status_code=404)

        # Resolve the candidate path and refuse anything outside the
        # frontend directory (path traversal).
        frontend_root = FRONTEND_DIR.resolve()
        candidate = (frontend_root / full_path).resolve()
        try:
            candidate.relative_to(frontend_root)
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index_html))


def main():
    """Entry point for the greenkube-api console script."""
    from greenkube.utils.log import configure_logging

    cfg = get_config()
    configure_logging(level=cfg.LOG_LEVEL, log_format=cfg.LOG_FORMAT)
    app = create_app(use_lifespan=True)
    uvicorn.run(
        app,
        host=cfg.API_HOST,
        port=cfg.API_PORT,
        proxy_headers=bool(cfg.TRUSTED_PROXY_IPS),
        forwarded_allow_ips=cfg.TRUSTED_PROXY_IPS,
        timeout_keep_alive=65,
    )
