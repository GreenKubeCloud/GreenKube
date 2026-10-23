# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import hashlib
import ipaddress
import logging
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Optional, Protocol, cast
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request

from greenkube.gitops.contracts import Operation, Plan
from greenkube.gitops.discovery.kubernetes import KubernetesDiscoverySource
from greenkube.gitops.discovery.runtime import GitOpsDiscoveryRuntime, create_gitops_discovery_runtime
from greenkube.storage.base_gitops_repository import GitOpsRepository
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
_WEBHOOK_API_PREFIX = "/api/v1/webhooks/"


def _is_webhook_ingress_path(path: str) -> bool:
    """Match only the one-segment provider webhook ingress route."""
    if not path.startswith(_WEBHOOK_API_PREFIX):
        return False
    provider_id = path[len(_WEBHOOK_API_PREFIX) :]
    return bool(provider_id) and "/" not in provider_id


def _route_path(request: Request) -> str:
    """Return the path FastAPI routes after removing a configured proxy prefix."""
    path = request.scope.get("path")
    if not isinstance(path, str):
        path = request.url.path

    root_path = request.scope.get("root_path")
    if not isinstance(root_path, str) or not root_path:
        root_path = getattr(request.app, "root_path", "")
    if not isinstance(root_path, str):
        return path

    prefix = root_path.rstrip("/")
    if prefix and (path == prefix or path.startswith(f"{prefix}/")):
        return path[len(prefix) :] or "/"
    return path


def verify_api_key(request: Request) -> None:
    """Verify the API key if ``GREENKUBE_API_KEY`` is configured.

    When the env var is empty the check is skipped (open access).
    Protected routes are ``/api/v1/*`` and ``/prometheus/metrics``; the
    SPA static files are always public. API-key exemptions are the single-
    provider webhook ingress route, liveness endpoint, docs, and OpenAPI
    schema. Credentials must be sent as ``Authorization: Bearer <key>`` and
    are compared in constant time.
    """
    from greenkube.core.config import get_config

    cfg = get_config()
    api_key = cfg.API_KEY

    path = _route_path(request)

    # SPA frontend and static assets never require the API key.
    if not path.startswith("/api/v1/") and path != "/prometheus/metrics":
        return

    # Health/service details stay protected; only the exact webhook route shape is exempt.
    if path in _PUBLIC_API_PATHS or (request.method == "POST" and _is_webhook_ingress_path(path)):
        return

    if getattr(cfg, "API_AUTH_MODE", "api_key") != "api_key":
        raise HTTPException(
            status_code=503,
            detail="The configured session/OIDC authentication contract is not available in this deployment.",
        )
    if not api_key:
        if getattr(cfg, "ENVIRONMENT", "development") == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured.")
        request.state.authenticated_actor = "anonymous"
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
    actor_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    request.state.authenticated_actor = f"api-key:{actor_digest}"


def get_authenticated_actor(request: Request) -> str:
    """Return a non-forgeable audit actor established by the auth dependency."""
    actor = getattr(request.state, "authenticated_actor", None)
    return actor if isinstance(actor, str) and actor else "anonymous"


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


def get_gitops_cluster(
    cluster: str | None = Query(default=None, min_length=1, description="Cluster whose GitOps bindings to inspect."),
) -> str:
    """Resolve an optional cluster filter to the configured cluster."""
    if cluster is not None:
        return cluster

    from greenkube.core.config import get_config

    return get_config().CLUSTER_NAME or "default"


async def get_gitops_discovery_source() -> KubernetesDiscoverySource:
    """Provide the read-only Kubernetes discovery source."""
    from greenkube.core.k8s_client import get_custom_objects_api

    custom_objects_api = await get_custom_objects_api()
    if custom_objects_api is None:
        raise HTTPException(status_code=503, detail="Kubernetes API configuration is unavailable.")
    return KubernetesDiscoverySource(custom_objects_api)


async def get_gitops_discovery_runtime(cluster: str = Depends(get_gitops_cluster)) -> GitOpsDiscoveryRuntime:
    """Provide a one-shot discovery runtime for operator-triggered refreshes."""
    from greenkube.core.db import get_db_manager

    return await create_gitops_discovery_runtime(db_manager=get_db_manager(), cluster=cluster)


async def get_gitops_binding_repository() -> GitOpsRepository:
    """Provide the GitOps workload-binding repository for the configured database."""
    from greenkube.core.config import get_config
    from greenkube.core.db import get_db_manager

    if get_config().DB_TYPE == "postgres":
        from greenkube.storage.postgres.gitops_repository import PostgresGitOpsRepository

        return PostgresGitOpsRepository(get_db_manager())

    from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository

    return SQLiteGitOpsRepository(get_db_manager())


# Hostnames that resolve to cloud metadata services and must never be configured.
_BLOCKED_SERVICE_HOSTS = {"metadata", "metadata.google.internal", "metadata.goog"}


def validate_service_url(url: str) -> str:
    """Validate a user-supplied backend service URL.

    Only the configured service hosts and explicit SERVICE_URL_ALLOWLIST hosts
    are accepted. This prevents a runtime API-key holder from redirecting
    collectors and their credentials to an arbitrary endpoint.
    """
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="URL must not be empty.")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="URL is invalid.") from None
    if parts.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must use the http or https scheme.")
    if parts.username or parts.password:
        raise HTTPException(status_code=400, detail="URL must not contain credentials.")
    try:
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port
    except ValueError:
        raise HTTPException(status_code=400, detail="URL host or port is invalid.") from None
    if not host:
        raise HTTPException(status_code=400, detail="URL must include a host.")
    if host in _BLOCKED_SERVICE_HOSTS:
        raise HTTPException(status_code=400, detail="URL host is not allowed.")

    from greenkube.core.config import get_config

    config = get_config()
    allowed_hosts = {
        item.strip().lower().rstrip(".")
        for item in getattr(config, "SERVICE_URL_ALLOWLIST", "").split(",")
        if item.strip()
    }
    for attribute in ("PROMETHEUS_URL", "OPENCOST_API_URL", "BOAVIZTA_API_URL"):
        configured_url = getattr(config, attribute, None)
        if not isinstance(configured_url, str) or not configured_url.strip():
            continue
        try:
            configured_host = urlsplit(configured_url).hostname
        except ValueError:
            continue
        if configured_host:
            allowed_hosts.add(configured_host.lower().rstrip("."))
    if host not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail="URL host must match a configured service host or SERVICE_URL_ALLOWLIST entry.",
        )

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


class PlanPreparationService(Protocol):
    """Resolve persisted recommendation actions, bindings and snapshots before planning."""

    async def prepare_plan(
        self,
        recommendation_ids: Sequence[int],
        *,
        actor: str = "system",
        values_mapping: Mapping[str, Any] | None = None,
    ) -> Plan:
        """Load persisted plan inputs and delegate validation/persistence to AutomationPlanService."""
        ...

    async def preview_plan(self, recommendation_ids: Sequence[int]) -> Plan:
        """Validate persisted plan inputs without persisting plan or action state."""
        ...


class AutomationPlanApiService(PlanPreparationService, Protocol):
    """Server-side plan API composition over AutomationPlanService and its queue."""

    async def expire_due_plans(self) -> list[Plan]: ...

    async def prune_retained_history(self, *, cutoff: datetime) -> dict[str, int]: ...

    async def read_plan(self, plan_id: str) -> Plan | None: ...

    async def read_operation(self, operation_id: str) -> Operation | None: ...

    async def read_operation_for_plan(self, plan_id: str) -> Operation | None: ...

    async def prepare_rollback(self, plan_id: str) -> Plan: ...

    async def approve_plan(self, plan_id: str, digest: str, *, actor: str = "system") -> Plan: ...

    async def mark_stale(
        self,
        plan_id: str,
        *,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None: ...


async def get_automation_plan_service(request: Request) -> AutomationPlanApiService | None:
    """Return the injected plan API composition, or ``None`` until it is configured.

    Production composition must provide persisted action lookup, resolved binding,
    provider snapshot and revision resolution. The API deliberately has no fallback
    that accepts repository content or source details from a request.
    """
    service = getattr(request.app.state, "automation_plan_service", None)
    return cast(AutomationPlanApiService | None, service)


async def get_summary_refresher():
    """Provide the shared dashboard summary refresher to API handlers."""
    from greenkube.core.factory import get_summary_refresher as factory_get_refresher

    return factory_get_refresher()
