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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from greenkube import __version__
from greenkube.api.routers import config as config_router
from greenkube.api.routers import metrics, namespaces, nodes, recommendations
from greenkube.core.config import config

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path("/app/frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    logger.info("🚀 Starting GreenKube API...")
    from greenkube.core.db import db_manager

    await db_manager.connect()
    logger.info("✅ Database connection established.")
    yield
    logger.info("🛑 Shutting down GreenKube API...")
    await db_manager.close()
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
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routers
    app.include_router(metrics.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(namespaces.router, prefix="/api/v1", tags=["Namespaces"])
    app.include_router(nodes.router, prefix="/api/v1", tags=["Nodes"])
    app.include_router(recommendations.router, prefix="/api/v1", tags=["Recommendations"])
    app.include_router(config_router.router, prefix="/api/v1", tags=["Config"])

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
    logging.basicConfig(
        level=config.LOG_LEVEL.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    app = create_app(use_lifespan=True)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
