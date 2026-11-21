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
from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_INTENSITY_YEAR

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
                content=HealthResponse(status="database_unavailable", version=__version__, demo=_demo()).model_dump(),
            )
        if not schema.current:
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="migrating", version=__version__, demo=_demo()).model_dump(),
            )
    supervisor = getattr(request.app.state, "automation_loop_supervisor", None)
    if supervisor is not None and not supervisor.health.ready():
        return JSONResponse(
            status_code=503,
            content=HealthResponse(status="degraded", version=__version__, demo=_demo()).model_dump(),
        )
    return HealthResponse(
        status="ok",
        version=__version__,
        collection=await _collection_status(),
        demo=_demo(),
        compression=await _compression_status(),
    )


def _demo() -> bool:
    """Whether this API runs `greenkube demo` (``GREENKUBE_DEMO_MODE``)."""
    return bool(getattr(get_config(), "GREENKUBE_DEMO_MODE", False))


async def _collection_status():
    """Watermark, lag and last hole of the collection (COL-04); ``None`` when unreadable.

    Bounded to one second and never changes the readiness answer.
    """
    import asyncio

    from greenkube.api.routers.collection import (
        collection_status,
        get_collection_watermark_repository,
        get_collection_window_repository,
    )

    try:
        return await asyncio.wait_for(
            collection_status(await get_collection_window_repository(), await get_collection_watermark_repository()),
            timeout=1.0,
        )
    except Exception as error:  # the table may not exist yet, or the database is down
        logger.debug("Collection status unavailable for /health (%s).", type(error).__name__)
        return None


async def _compression_status():
    """Compression watermark and lag (ROL-01); ``None`` when unreadable.

    Bounded to one second; a lag over two hours reports ``degraded`` here and in
    ``greenkube_metrics_compression_lag_seconds`` but never changes readiness.
    """
    import asyncio

    from greenkube.api.schemas import CompressionStatusResponse
    from greenkube.core.db import get_db_manager
    from greenkube.core.metrics_compressor import MetricsCompressor

    try:
        compressor = MetricsCompressor(get_db_manager(), collection_watermark_provider=None)
        status = await asyncio.wait_for(compressor.compression_status(), timeout=1.0)
    except Exception as error:  # the table may not exist yet, or the database is down
        logger.debug("Compression status unavailable for /health (%s).", type(error).__name__)
        return None
    return CompressionStatusResponse(
        status=status["status"],
        lag_seconds=status["lag_seconds"],
        watermarks={table: info["watermark"] for table, info in status["tables"].items()},
    )


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
        intensity_defaults_year=DEFAULT_INTENSITY_YEAR,
    )
