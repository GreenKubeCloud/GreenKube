# src/greenkube/api/routers/health.py
"""
API routes for service health checks and runtime configuration updates.

Provides endpoints to check the health of all data sources (Prometheus,
OpenCost, Electricity Maps, Boavizta, Kubernetes) and to update service
URLs at runtime from the frontend.
"""

import logging
import os

from fastapi import APIRouter, HTTPException

from greenkube.core.config import get_config
from greenkube.core.health import invalidate_health_cache, run_health_checks
from greenkube.models.health import (
    HealthCheckResponse,
    ServiceConfigUpdate,
    ServiceHealth,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health/services", response_model=HealthCheckResponse)
async def get_services_health(force: bool = False):
    """Return health status for all data sources.

    Pass ``?force=true`` to bypass the cache and force fresh probes.
    """
    return await run_health_checks(force=force)


@router.get("/health/services/{service_name}", response_model=ServiceHealth)
async def get_service_health(service_name: str, force: bool = False):
    """Return health status for a single data source by name."""
    result = await run_health_checks(force=force)
    service = result.services.get(service_name)
    if not service:
        valid = list(result.services.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_name}' not found. Valid services: {valid}",
        )
    return service


@router.post("/config/services", response_model=HealthCheckResponse)
async def update_service_config(update: ServiceConfigUpdate):
    """Update service URLs or tokens at runtime from the frontend.

    Changes are applied to the running process via environment variables
    and the Config singleton is reloaded. They do NOT persist across
    pod restarts — for permanent changes, update the Helm values or
    environment variables.

    After applying changes the health cache is invalidated and a fresh
    health check is executed and returned.
    """
    cfg = get_config()
    changed = False

    if update.prometheus_url is not None:
        os.environ["PROMETHEUS_URL"] = update.prometheus_url
        logger.info("Prometheus URL updated to: %s", update.prometheus_url)
        changed = True

    if update.opencost_url is not None:
        os.environ["OPENCOST_API_URL"] = update.opencost_url
        logger.info("OpenCost URL updated to: %s", update.opencost_url)
        changed = True

    if update.electricity_maps_token is not None:
        os.environ["ELECTRICITY_MAPS_TOKEN"] = update.electricity_maps_token
        logger.info("Electricity Maps token updated.")
        changed = True

    if update.boavizta_url is not None:
        os.environ["BOAVIZTA_API_URL"] = update.boavizta_url
        logger.info("Boavizta URL updated to: %s", update.boavizta_url)
        changed = True

    if changed:
        cfg.reload()
        invalidate_health_cache()
        logger.info("Configuration reloaded after service config update.")

    return await run_health_checks(force=True)
