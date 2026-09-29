# src/greenkube/api/routers/health.py
"""
API routes for service health checks and runtime configuration updates.

Provides endpoints to check the health of all data sources (Prometheus,
OpenCost, Electricity Maps, Boavizta, Kubernetes) and to update service
URLs at runtime from the frontend.
"""

import logging

from fastapi import APIRouter, HTTPException, Response

from greenkube.api.dependencies import validate_service_url
from greenkube.core.config import get_config_service
from greenkube.core.health import invalidate_health_cache, run_health_checks
from greenkube.core.k8s_secret_store import patch_k8s_secret
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
async def update_service_config(update: ServiceConfigUpdate, response: Response):
    """Update service URLs or tokens at runtime from the frontend.

    Changes are applied to the running process via environment variables
    and the Config singleton is reloaded. They do NOT persist across
    pod restarts — for permanent changes, update the Helm values or
    environment variables.

    After applying changes the health cache is invalidated and a fresh
    health check is executed and returned.
    """
    service = get_config_service()
    updates: dict[str, str] = {}

    if update.prometheus_url is not None:
        validate_service_url(update.prometheus_url)
        updates["PROMETHEUS_URL"] = update.prometheus_url
        logger.info("Prometheus URL updated to: %s", update.prometheus_url)

    if update.opencost_url is not None:
        validate_service_url(update.opencost_url)
        updates["OPENCOST_API_URL"] = update.opencost_url
        logger.info("OpenCost URL updated to: %s", update.opencost_url)

    if update.electricity_maps_token is not None:
        updates["ELECTRICITY_MAPS_TOKEN"] = update.electricity_maps_token
        logger.info("Electricity Maps token updated.")

    if update.boavizta_url is not None:
        validate_service_url(update.boavizta_url)
        updates["BOAVIZTA_API_URL"] = update.boavizta_url
        logger.info("Boavizta URL updated to: %s", update.boavizta_url)

    if update.wattnet_email is not None:
        updates["WATTNET_EMAIL"] = update.wattnet_email
        logger.info("Wattnet email updated.")

    if update.wattnet_password is not None:
        updates["WATTNET_PASSWORD"] = update.wattnet_password
        logger.info("Wattnet password updated.")

    if updates:
        acknowledgement = await service.apply(updates, persist=False)
        invalidate_health_cache()
        logger.info("Configuration reloaded after service config update.")

        # Mounted external secrets remain read-only. The K8s API patch is a
        # separate, best-effort persistence channel for runtime overrides.
        persisted = await patch_k8s_secret(updates)
        response.headers["X-Configuration-Version"] = str(acknowledgement.version)
        response.headers["X-Configuration-Persisted"] = str(persisted).lower()
        if not persisted:
            logger.warning(
                "Runtime configuration version %s was applied in memory only.",
                acknowledgement.version,
            )
    return await run_health_checks(force=True)
