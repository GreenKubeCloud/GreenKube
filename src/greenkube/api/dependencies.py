# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import logging
import re
from typing import Optional

from fastapi import HTTPException, Query

from greenkube.storage.base_repository import CarbonIntensityRepository, NodeRepository, RecommendationRepository

logger = logging.getLogger(__name__)

# Valid Kubernetes namespace pattern: lowercase alphanumeric + hyphens, 1-63 chars.
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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


async def get_node_repository() -> NodeRepository:
    """Provides the NodeRepository instance via the factory."""
    from greenkube.core.factory import get_node_repository as factory_get_node_repo

    return factory_get_node_repo()


async def get_recommendation_repository() -> RecommendationRepository:
    """Provides the RecommendationRepository instance via the factory."""
    from greenkube.core.factory import get_recommendation_repository as factory_get_reco_repo

    return factory_get_reco_repo()
