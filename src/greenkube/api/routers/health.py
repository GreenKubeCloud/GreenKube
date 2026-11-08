# src/greenkube/api/routers/health.py
"""
API routes for data-source health checks.

Provides endpoints to check the health of all data sources (Prometheus,
OpenCost, Electricity Maps, Boavizta, Kubernetes). Data-source endpoints and
tokens are configuration as code (Helm values and Secrets, roadmap A13): the
API reports them read-only and never writes them.
"""

import logging

from fastapi import APIRouter, HTTPException

from greenkube.core.health import run_health_checks
from greenkube.models.health import HealthCheckResponse, ServiceHealth

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


@router.post("/config/services", include_in_schema=False, status_code=410)
async def update_service_config_removed():
    """Removed (roadmap A13, BUG-11, BUG-60): data sources are configuration as code.

    Runtime edits reached only the API process while the controller kept its
    collectors, and persisting them into the release Secret drifted from Git.
    Set the URLs and tokens in Helm values or Secrets instead.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "CONFIGURATION_AS_CODE",
            "message": (
                "Data-source URLs and tokens are configured with Helm values and Secrets; "
                "see the Settings page for the matching Helm snippets."
            ),
        },
    )
