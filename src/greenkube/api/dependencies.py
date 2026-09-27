# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import logging
import re
import secrets
from typing import Optional

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
_PUBLIC_API_PATHS = ("/api/v1/health", "/api/v1/docs", "/api/v1/openapi.json")


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

    api_key = get_config().API_KEY
    if not api_key:
        return  # no key configured → open access

    path = request.url.path

    # SPA frontend and static assets never require the API key.
    if not path.startswith("/api/v1/") and path != "/prometheus/metrics":
        return

    # Public operational endpoints (exact match: /health/services is not public).
    if path in _PUBLIC_API_PATHS:
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
