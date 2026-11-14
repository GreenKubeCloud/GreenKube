# src/greenkube/api/routers/config.py
"""
API routes for exposing non-sensitive configuration and version information.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from greenkube import __version__
from greenkube.api.schemas import ConfigResponse, HealthResponse, VersionResponse
from greenkube.core.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
async def health(request: Request):
    """Readiness of an API replica.

    503 ``migrating`` until the controller has applied every shipped migration (F11:
    only the controller migrates; the answer is cached once current, so a database
    blip never takes every replica out of rotation), and 503 ``degraded`` when a
    supervised in-process loop is wedged (D9).
    """
    if getattr(request.app.state, "check_schema", False):
        from greenkube.core.db import get_db_manager

        try:
            schema = await get_db_manager().schema_status()
        except Exception as error:
            logger.warning("Schema readiness check failed (%s).", type(error).__name__)
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="database_unavailable", version=__version__).model_dump(),
            )
        if not schema.current:
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="migrating", version=__version__).model_dump(),
            )
    supervisor = getattr(request.app.state, "automation_loop_supervisor", None)
    if supervisor is not None and not supervisor.health.ready():
        return JSONResponse(
            status_code=503,
            content=HealthResponse(status="degraded", version=__version__).model_dump(),
        )
    return HealthResponse(status="ok", version=__version__)


@router.get("/version", response_model=VersionResponse)
async def version():
    """Return the current application version."""
    return VersionResponse(version=__version__)


@router.get("/config", response_model=ConfigResponse)
async def get_config_endpoint():
    """Return non-sensitive configuration values.

    Secrets (tokens, passwords, connection strings) are never exposed.
    """
    cfg = get_config()
    return ConfigResponse(
        db_type=cfg.DB_TYPE,
        cloud_provider=cfg.CLOUD_PROVIDER,
        default_zone=cfg.DEFAULT_ZONE,
        default_intensity=cfg.DEFAULT_INTENSITY,
        default_pue=cfg.DEFAULT_PUE,
        log_level=cfg.LOG_LEVEL,
        log_format=cfg.LOG_FORMAT,
        normalization_granularity=cfg.NORMALIZATION_GRANULARITY,
        prometheus_query_range_step=cfg.PROMETHEUS_QUERY_RANGE_STEP,
        api_host=cfg.API_HOST,
        api_port=cfg.API_PORT,
        currency=str(getattr(cfg, "IMPACT_PRICING_CURRENCY", "USD") or "USD"),
    )
