# src/greenkube/api/app.py
"""
FastAPI application factory for the GreenKube API.

Uses the factory pattern so the app can be created with or without
lifespan management (e.g., tests skip DB initialization).
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from greenkube import __version__
from greenkube.api.routers import config as config_router
from greenkube.api.routers import metrics, nodes, recommendations
from greenkube.core.config import config

logger = logging.getLogger(__name__)


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
        lifespan=lifespan if use_lifespan else None,
    )

    # Register routers
    app.include_router(metrics.router, prefix="/api/v1", tags=["Metrics"])
    app.include_router(nodes.router, prefix="/api/v1", tags=["Nodes"])
    app.include_router(recommendations.router, prefix="/api/v1", tags=["Recommendations"])
    app.include_router(config_router.router, prefix="/api/v1", tags=["Config"])

    return app


def main():
    """Entry point for the greenkube-api console script."""
    logging.basicConfig(
        level=config.LOG_LEVEL.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    app = create_app(use_lifespan=True)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
