# src/greenkube/api/routers/config.py
"""
API routes for exposing non-sensitive configuration and version information.
"""

import logging

from fastapi import APIRouter

from greenkube import __version__
from greenkube.api.schemas import ConfigResponse, HealthResponse, VersionResponse
from greenkube.core.config import config

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/version", response_model=VersionResponse)
async def version():
    """Return the current application version."""
    return VersionResponse(version=__version__)


@router.get("/config", response_model=ConfigResponse)
async def get_config():
    """Return non-sensitive configuration values.

    Secrets (tokens, passwords, connection strings) are never exposed.
    """
    return ConfigResponse(
        db_type=config.DB_TYPE,
        cloud_provider=config.CLOUD_PROVIDER,
        default_zone=config.DEFAULT_ZONE,
        default_intensity=config.DEFAULT_INTENSITY,
        default_pue=config.DEFAULT_PUE,
        log_level=config.LOG_LEVEL,
        normalization_granularity=config.NORMALIZATION_GRANULARITY,
        prometheus_query_range_step=config.PROMETHEUS_QUERY_RANGE_STEP,
        api_host=config.API_HOST,
        api_port=config.API_PORT,
    )
