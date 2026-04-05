# src/greenkube/core/health.py
"""
Health check service for all GreenKube data sources.

Performs connectivity checks against Prometheus, OpenCost, Electricity Maps,
Boavizta, and Kubernetes, returning a structured health report. The service
is infrastructure-agnostic and delegates HTTP probes to the shared async
HTTP client.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from greenkube.core.config import Config, get_config
from greenkube.models.health import (
    HealthCheckResponse,
    ServiceHealth,
    ServiceStatus,
)
from greenkube.utils.http_client import get_async_http_client

logger = logging.getLogger(__name__)

# Cache the last health check result to avoid hammering external services.
_cached_result: Optional[HealthCheckResponse] = None
_cached_at: float = 0.0
CACHE_TTL_SECONDS = 30.0


async def check_prometheus(cfg: Config) -> ServiceHealth:
    """Check Prometheus connectivity by querying the 'up' metric."""
    name = "prometheus"
    url = cfg.PROMETHEUS_URL

    if not url:
        # Attempt service discovery
        try:
            from greenkube.collectors.discovery.prometheus import PrometheusDiscovery

            discovered_url = await PrometheusDiscovery().discover()
            if discovered_url:
                url = discovered_url
                return await _probe_prometheus(url, configured=False, discovered=True)
        except Exception as exc:
            logger.debug("Prometheus discovery failed during health check: %s", exc)

        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNCONFIGURED,
            message="Prometheus URL is not configured and service discovery failed.",
            last_check=datetime.now(timezone.utc),
        )

    return await _probe_prometheus(url, configured=True, discovered=False)


async def _probe_prometheus(url: str, configured: bool, discovered: bool) -> ServiceHealth:
    """Probe a Prometheus endpoint and return health status."""
    name = "prometheus"
    probe_url = f"{url.rstrip('/')}/api/v1/query"
    start = time.monotonic()
    try:
        async with get_async_http_client(verify=get_config().PROMETHEUS_VERIFY_CERTS) as client:
            resp = await client.get(probe_url, params={"query": "up"}, timeout=5.0)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                try:
                    body = resp.json()
                    if body.get("status") == "success":
                        return ServiceHealth(
                            name=name,
                            status=ServiceStatus.HEALTHY,
                            url=url,
                            message="Prometheus is reachable and responding.",
                            latency_ms=round(latency, 1),
                            last_check=datetime.now(timezone.utc),
                            configured=configured,
                            discovered=discovered,
                        )
                except Exception:
                    pass

            return ServiceHealth(
                name=name,
                status=ServiceStatus.DEGRADED,
                url=url,
                message=f"Prometheus returned status {resp.status_code}.",
                latency_ms=round(latency, 1),
                last_check=datetime.now(timezone.utc),
                configured=configured,
                discovered=discovered,
            )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNREACHABLE,
            url=url,
            message=f"Cannot reach Prometheus: {exc}",
            latency_ms=round(latency, 1),
            last_check=datetime.now(timezone.utc),
            configured=configured,
            discovered=discovered,
        )


async def check_opencost(cfg: Config) -> ServiceHealth:
    """Check OpenCost connectivity via its /healthz endpoint."""
    name = "opencost"
    url = cfg.OPENCOST_API_URL

    if not url:
        # Attempt service discovery
        try:
            from greenkube.collectors.discovery.opencost import OpenCostDiscovery

            discovered_url = await OpenCostDiscovery().discover()
            if discovered_url:
                url = discovered_url
                return await _probe_opencost(url, configured=False, discovered=True)
        except Exception as exc:
            logger.debug("OpenCost discovery failed during health check: %s", exc)

        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNCONFIGURED,
            message="OpenCost URL is not configured and service discovery failed.",
            last_check=datetime.now(timezone.utc),
        )

    return await _probe_opencost(url, configured=True, discovered=False)


async def _probe_opencost(url: str, configured: bool, discovered: bool) -> ServiceHealth:
    """Probe an OpenCost endpoint and return health status."""
    name = "opencost"
    probe_url = f"{url.rstrip('/')}/healthz"
    start = time.monotonic()
    try:
        async with get_async_http_client(verify=get_config().OPENCOST_VERIFY_CERTS) as client:
            resp = await client.get(probe_url, timeout=5.0)
            latency = (time.monotonic() - start) * 1000

            if 200 <= resp.status_code < 300:
                return ServiceHealth(
                    name=name,
                    status=ServiceStatus.HEALTHY,
                    url=url,
                    message="OpenCost is reachable and healthy.",
                    latency_ms=round(latency, 1),
                    last_check=datetime.now(timezone.utc),
                    configured=configured,
                    discovered=discovered,
                )

            return ServiceHealth(
                name=name,
                status=ServiceStatus.DEGRADED,
                url=url,
                message=f"OpenCost returned status {resp.status_code}.",
                latency_ms=round(latency, 1),
                last_check=datetime.now(timezone.utc),
                configured=configured,
                discovered=discovered,
            )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNREACHABLE,
            url=url,
            message=f"Cannot reach OpenCost: {exc}",
            latency_ms=round(latency, 1),
            last_check=datetime.now(timezone.utc),
            configured=configured,
            discovered=discovered,
        )


async def check_electricity_maps(cfg: Config) -> ServiceHealth:
    """Check Electricity Maps API token and connectivity."""
    name = "electricity_maps"

    if not cfg.ELECTRICITY_MAPS_TOKEN:
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNCONFIGURED,
            url="https://api.electricitymaps.com/v3",
            message="Electricity Maps API token is not set. Using static fallback data.",
            last_check=datetime.now(timezone.utc),
        )

    probe_url = f"https://api.electricitymaps.com/v3/carbon-intensity/latest?zone={cfg.DEFAULT_ZONE}"
    headers = {"auth-token": cfg.ELECTRICITY_MAPS_TOKEN}
    start = time.monotonic()
    try:
        async with get_async_http_client() as client:
            resp = await client.get(probe_url, headers=headers, timeout=5.0)
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return ServiceHealth(
                    name=name,
                    status=ServiceStatus.HEALTHY,
                    url="https://api.electricitymaps.com/v3",
                    message="Electricity Maps API is reachable with a valid token.",
                    latency_ms=round(latency, 1),
                    last_check=datetime.now(timezone.utc),
                    configured=True,
                )
            if resp.status_code == 401:
                return ServiceHealth(
                    name=name,
                    status=ServiceStatus.DEGRADED,
                    url="https://api.electricitymaps.com/v3",
                    message="Electricity Maps API returned 401 — token may be invalid.",
                    latency_ms=round(latency, 1),
                    last_check=datetime.now(timezone.utc),
                    configured=True,
                )
            return ServiceHealth(
                name=name,
                status=ServiceStatus.DEGRADED,
                url="https://api.electricitymaps.com/v3",
                message=f"Electricity Maps API returned status {resp.status_code}.",
                latency_ms=round(latency, 1),
                last_check=datetime.now(timezone.utc),
                configured=True,
            )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNREACHABLE,
            url="https://api.electricitymaps.com/v3",
            message=f"Cannot reach Electricity Maps API: {exc}",
            latency_ms=round(latency, 1),
            last_check=datetime.now(timezone.utc),
            configured=True,
        )


async def check_boavizta(cfg: Config) -> ServiceHealth:
    """Check Boavizta API connectivity."""
    name = "boavizta"
    url = cfg.BOAVIZTA_API_URL

    if not url:
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNCONFIGURED,
            message="Boavizta API URL is not configured.",
            last_check=datetime.now(timezone.utc),
        )

    probe_url = f"{url.rstrip('/')}/v1/server/"
    start = time.monotonic()
    try:
        async with get_async_http_client() as client:
            resp = await client.get(probe_url, timeout=5.0)
            latency = (time.monotonic() - start) * 1000

            if 200 <= resp.status_code < 400:
                return ServiceHealth(
                    name=name,
                    status=ServiceStatus.HEALTHY,
                    url=url,
                    message="Boavizta API is reachable.",
                    latency_ms=round(latency, 1),
                    last_check=datetime.now(timezone.utc),
                    configured=True,
                )

            return ServiceHealth(
                name=name,
                status=ServiceStatus.DEGRADED,
                url=url,
                message=f"Boavizta API returned status {resp.status_code}.",
                latency_ms=round(latency, 1),
                last_check=datetime.now(timezone.utc),
                configured=True,
            )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNREACHABLE,
            url=url,
            message=f"Cannot reach Boavizta API: {exc}",
            latency_ms=round(latency, 1),
            last_check=datetime.now(timezone.utc),
            configured=True,
        )


async def check_kubernetes() -> ServiceHealth:
    """Check Kubernetes API connectivity."""
    name = "kubernetes"
    start = time.monotonic()
    try:
        from greenkube.core.k8s_client import get_core_v1_api

        api = await get_core_v1_api()
        if not api:
            return ServiceHealth(
                name=name,
                status=ServiceStatus.UNREACHABLE,
                message="Kubernetes client could not be initialized.",
                last_check=datetime.now(timezone.utc),
            )

        # Quick version check to validate connectivity
        from kubernetes_asyncio import client as k8s_client

        async with k8s_client.ApiClient() as api_client:
            version_api = k8s_client.VersionApi(api_client)
            version = await version_api.get_code()
            latency = (time.monotonic() - start) * 1000
            return ServiceHealth(
                name=name,
                status=ServiceStatus.HEALTHY,
                message=f"Kubernetes API is reachable (v{version.git_version}).",
                latency_ms=round(latency, 1),
                last_check=datetime.now(timezone.utc),
                configured=True,
            )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ServiceHealth(
            name=name,
            status=ServiceStatus.UNREACHABLE,
            message=f"Cannot reach Kubernetes API: {exc}",
            latency_ms=round(latency, 1),
            last_check=datetime.now(timezone.utc),
        )


async def run_health_checks(force: bool = False) -> HealthCheckResponse:
    """Run all health checks in parallel and return an aggregated response.

    Results are cached for CACHE_TTL_SECONDS to avoid excessive probing.
    Pass force=True to bypass the cache.
    """
    global _cached_result, _cached_at

    now = time.monotonic()
    if not force and _cached_result and (now - _cached_at) < CACHE_TTL_SECONDS:
        return _cached_result

    cfg = get_config()

    results = await asyncio.gather(
        check_prometheus(cfg),
        check_opencost(cfg),
        check_electricity_maps(cfg),
        check_boavizta(cfg),
        check_kubernetes(),
        return_exceptions=True,
    )

    services = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error("Health check raised an exception: %s", result)
            continue
        if isinstance(result, ServiceHealth):
            services[result.name] = result

    # Determine overall status
    statuses = [s.status for s in services.values()]
    if all(s == ServiceStatus.HEALTHY for s in statuses):
        overall = "ok"
    elif any(s == ServiceStatus.UNREACHABLE for s in statuses):
        overall = "degraded"
    else:
        overall = "degraded"

    from greenkube import __version__

    response = HealthCheckResponse(
        status=overall,
        version=__version__,
        services=services,
    )

    _cached_result = response
    _cached_at = time.monotonic()

    return response


def invalidate_health_cache() -> None:
    """Invalidate the health check cache, forcing a fresh check on next call."""
    global _cached_result, _cached_at
    _cached_result = None
    _cached_at = 0.0
