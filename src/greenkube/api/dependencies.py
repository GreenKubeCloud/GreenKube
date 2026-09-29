# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import ipaddress
import logging
import re
import secrets
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Query, Request

from greenkube.storage.base_repository import (
    CarbonIntensityRepository,
    CombinedMetricsRepository,
    NodeRepository,
    RecommendationRepository,
    SummaryRepository,
    TimeseriesCacheRepository,
)
from greenkube.storage.base_savings_repository import SavingsLedgerRepository

logger = logging.getLogger(__name__)

# Valid Kubernetes namespace pattern: lowercase alphanumeric + hyphens, 1-63 chars.
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


# Public API paths that never require the API key (exact matches only).
_PUBLIC_API_PATHS = (
    "/api/v1/health",
    "/api/v1/health/heartbeat",
    "/api/v1/docs",
    "/api/v1/openapi.json",
)


def verify_api_key(request: Request) -> None:
    """Verify the API key if ``GREENKUBE_API_KEY`` is configured.

    When the env var is empty the check is skipped (open access).
    Protected routes are ``/api/v1/*`` and ``/prometheus/metrics``; the
    SPA static files are always public. Only three exact paths are exempt
    from authentication: the liveness endpoint, the docs and the OpenAPI
    schema. Credentials must be sent as ``Authorization: Bearer <key>`` and
    are compared in constant time.
    """
    from greenkube.core.config import get_config

    cfg = get_config()
    api_key = cfg.API_KEY

    path = request.url.path

    # SPA frontend and static assets never require the API key.
    if not path.startswith("/api/v1/") and path != "/prometheus/metrics":
        return

    # Public operational endpoints (exact match: /health/services is not public).
    if path in _PUBLIC_API_PATHS:
        return

    if getattr(cfg, "API_AUTH_MODE", "api_key") != "api_key":
        raise HTTPException(
            status_code=503,
            detail="The configured session/OIDC authentication contract is not available in this deployment.",
        )
    if not api_key:
        if getattr(cfg, "ENVIRONMENT", "development") == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured.")
        return

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(token, api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_namespace(
    namespace: Optional[str] = Query(None, description="Filter by Kubernetes namespace."),
) -> Optional[str]:
    """Validate the optional namespace query parameter.

    Returns the namespace unchanged, or raises 400 if invalid.
    """
    if namespace is not None and not _NAMESPACE_RE.match(namespace):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid namespace '{namespace}'. "
                "Must match Kubernetes naming rules (lowercase alphanumeric and hyphens, 1-63 chars)."
            ),
        )
    return namespace


# Hostnames that resolve to cloud metadata services and must never be configured.
_BLOCKED_SERVICE_HOSTS = {"metadata", "metadata.google.internal", "metadata.goog"}


def validate_service_url(url: str) -> str:
    """Validate a user-supplied backend service URL.

    In-cluster (private) and public ``http(s)`` endpoints are allowed. The
    following are rejected because they are classic SSRF targets:
    non-http(s) schemes, embedded credentials, loopback, link-local,
    multicast, unspecified and CGNAT addresses, and known cloud metadata
    hostnames. Hostnames are not resolved here so that in-cluster service
    names keep working even when DNS is not reachable from the API pod.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must use the http or https scheme.")
    if parts.username or parts.password:
        raise HTTPException(status_code=400, detail="URL must not contain credentials.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail="URL must include a host.")
    if host in _BLOCKED_SERVICE_HOSTS:
        raise HTTPException(status_code=400, detail="URL host is not allowed.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url

    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise HTTPException(
            status_code=400,
            detail="URL must not target loopback, link-local or multicast addresses.",
        )
    if not (ip.is_private or ip.is_global):
        raise HTTPException(status_code=400, detail="URL must target a private or public address.")
    return url


async def get_carbon_repository() -> CarbonIntensityRepository:
    """Provides the CarbonIntensityRepository instance via the factory."""
    from greenkube.core.factory import get_repository

    return get_repository()


async def get_combined_metrics_repository() -> CombinedMetricsRepository:
    """Provides the CombinedMetricsRepository instance via the factory."""
    from greenkube.core.factory import get_combined_metrics_repository as factory_get_combined

    return factory_get_combined()


async def get_node_repository() -> NodeRepository:
    """Provides the NodeRepository instance via the factory."""
    from greenkube.core.factory import get_node_repository as factory_get_node_repo

    return factory_get_node_repo()


async def get_recommendation_repository() -> RecommendationRepository:
    """Provides the RecommendationRepository instance via the factory."""
    from greenkube.core.factory import get_recommendation_repository as factory_get_reco_repo

    return factory_get_reco_repo()


async def get_pull_request_repository():
    """Provides the PullRequestRepository instance via the factory."""
    from greenkube.core.factory import get_pull_request_repository as factory_get_pr_repo

    return factory_get_pr_repo()


async def get_savings_ledger_repository() -> SavingsLedgerRepository:
    """Provides the SavingsLedgerRepository instance via the factory."""
    from greenkube.core.factory import get_savings_ledger_repository as factory_get_savings_repo

    return factory_get_savings_repo()


async def get_summary_repository() -> SummaryRepository:
    """Provides the SummaryRepository instance via the factory."""
    from greenkube.core.factory import get_summary_repository as factory_get_summary

    return factory_get_summary()


async def get_timeseries_cache_repository() -> TimeseriesCacheRepository:
    """Provides the TimeseriesCacheRepository instance via the factory."""
    from greenkube.core.factory import get_timeseries_cache_repository as factory_get_ts

    return factory_get_ts()


async def get_optimization_engine():
    """Provide the shared optimization engine to API handlers."""
    from greenkube.core.factory import get_optimization_engine as factory_get_engine

    return factory_get_engine()


async def get_automation_service():
    """Provide the shared recommendation automation service to API handlers."""
    from greenkube.core.factory import get_automation_service as factory_get_service

    return factory_get_service()


async def get_summary_refresher():
    """Provide the shared dashboard summary refresher to API handlers."""
    from greenkube.core.factory import get_summary_refresher as factory_get_refresher

    return factory_get_refresher()
