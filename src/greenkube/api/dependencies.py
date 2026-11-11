# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import hashlib
import logging
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Optional, Protocol, cast

from fastapi import Depends, HTTPException, Query, Request

from greenkube.auth.http import CSRF_HEADER, is_same_origin
from greenkube.auth.metrics import record_auth_failure
from greenkube.auth.principal import ALL_SCOPES, ANONYMOUS, SAFE_METHODS, Principal, Role, required_scope
from greenkube.auth.service import AuthService, api_key_principal, user_principal
from greenkube.auth.settings import AuthSettings
from greenkube.auth.tokens import constant_time_equals, sha256_hex
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
# API versions resolved by :func:`authenticate` (``/api/v2`` serves changes and measurements).
_PROTECTED_API_PREFIXES = ("/api/v1/", "/api/v2/")


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

    if getattr(cfg, "API_AUTH_MODE", "api_key") not in ("api_key", "session"):
        raise HTTPException(
            status_code=503,
            detail="The configured OIDC authentication contract is not available in this deployment.",
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


# Auth endpoints reachable without credentials: they resolve a principal when
# one is presented but never require it.
_AUTH_PUBLIC_PATHS = ("/api/v1/auth/login", "/api/v1/auth/logout")
# What a user who must still change the initial password may call.
_PASSWORD_CHANGE_ALLOWED = ("/api/v1/auth/me", "/api/v1/auth/password", "/api/v1/auth/logout", "/api/v1/version")
_UNAUTHORIZED = "Authentication required."
_INVALID_KEY = "Invalid or missing API key."


def get_auth_service(request: Request) -> AuthService | None:
    """The account and session service, available once the API lifespan connected the database."""
    service = getattr(request.app.state, "auth_service", None)
    return service if isinstance(service, AuthService) else None


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": message})


def _bearer_unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail=_INVALID_KEY, headers={"WWW-Authenticate": "Bearer"})


def _legacy_principal(token: str) -> Principal:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return Principal(
        kind="apikey",
        name="legacy",
        role=Role.ADMIN.value,
        scopes=ALL_SCOPES,
        legacy=True,
        actor_override=f"api-key:{digest}",
    )


async def _bearer_principal(request: Request, settings: AuthSettings, service: AuthService | None) -> Principal:
    """Resolve ``Authorization: Bearer``: the deprecated bootstrap key, then a scoped key."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() == "bearer" and token:
        # Compare digests so that neither the content nor the length of the key leaks through timing.
        if settings.legacy_api_key and constant_time_equals(sha256_hex(token), sha256_hex(settings.legacy_api_key)):
            return _legacy_principal(token)
        if service is not None and token.startswith("gk_"):
            key = await service.resolve_api_key(token)
            if key is not None:
                return api_key_principal(key)
    if await _open_access(settings, service):
        # Nothing to check a key against on an open development instance.
        return ANONYMOUS
    record_auth_failure("invalid_api_key")
    raise _bearer_unauthorized()


async def _session_principal(request: Request, settings: AuthSettings, service: AuthService | None) -> Principal | None:
    if not settings.sessions_enabled or service is None:
        return None
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    resolved = await service.resolve_session(token)
    if resolved is None:
        record_auth_failure("invalid_session")
        return None
    session, user = resolved
    request.state.auth_session = session
    return user_principal(user, session, token)


async def _open_access(settings: AuthSettings, service: AuthService | None) -> bool:
    """Whether an unauthenticated request is served anonymously.

    Only a development instance with no bootstrap API key and no account is
    open, as before sessions existed. Production never is.
    """
    if settings.legacy_api_key or settings.environment == "production":
        return False
    if settings.mode == "api_key" or service is None:
        return True
    try:
        return not await service.users_exist()
    except Exception:
        logger.warning("Could not check whether accounts exist; refusing anonymous access.")
        return False


def _remember(request: Request, principal: Principal) -> None:
    request.state.principal = principal
    request.state.authenticated_actor = principal.actor


async def authenticate(request: Request) -> None:
    """Resolve the request's :class:`Principal` and enforce scopes and CSRF (F14, F15).

    Order: a Bearer API key (the deprecated ``GREENKUBE_API_KEY`` bootstrap key,
    then scoped ``gk_`` keys), then the session cookie. Protected routes are
    ``/api/v1/*``, ``/api/v2/*`` and ``/prometheus/metrics``; the SPA, liveness, docs, OpenAPI
    schema and the provider webhook ingress route are public. Unsafe methods
    authenticated by a session cookie need a same-origin ``Origin``/``Referer``
    and an ``X-CSRF-Token`` header matching the session.
    """
    from greenkube.core.config import get_config

    path = _route_path(request)
    if not path.startswith(_PROTECTED_API_PREFIXES) and path != "/prometheus/metrics":
        return
    if path in _PUBLIC_API_PATHS or (request.method == "POST" and _is_webhook_ingress_path(path)):
        return

    settings = AuthSettings.from_config(get_config())
    if settings.mode not in ("api_key", "session"):
        raise HTTPException(
            status_code=503,
            detail="The configured OIDC authentication contract is not available in this deployment.",
        )
    service = get_auth_service(request)
    auth_public = path in _AUTH_PUBLIC_PATHS

    if request.headers.get("Authorization"):
        principal: Principal | None = await _bearer_principal(request, settings, service)
    else:
        principal = await _session_principal(request, settings, service)

    if principal is None:
        if auth_public:
            return
        if await _open_access(settings, service):
            principal = ANONYMOUS
        elif settings.environment == "production" and not settings.legacy_api_key and service is None:
            raise HTTPException(status_code=503, detail="API authentication is not configured.")
        else:
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})

    _remember(request, principal)
    if auth_public:
        return

    if principal.must_change_password and path not in _PASSWORD_CHANGE_ALLOWED:
        raise _forbidden("PASSWORD_CHANGE_REQUIRED", "Change the initial password before using GreenKube.")

    scope = required_scope(request.method, path)
    if scope is not None and not principal.has_scope(scope):
        record_auth_failure("forbidden_scope")
        raise _forbidden("INSUFFICIENT_SCOPE", f"This credential lacks the '{scope}' scope.")

    if principal.is_session and request.method.upper() not in SAFE_METHODS:
        if not is_same_origin(request, settings, require=True) or not constant_time_equals(
            request.headers.get(CSRF_HEADER), principal.csrf_token
        ):
            record_auth_failure("csrf")
            raise _forbidden("CSRF_REJECTED", "Cross-site request rejected: reload the page and try again.")


def get_principal(request: Request) -> Principal:
    """The principal resolved by :func:`authenticate` (401 when there is none)."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    return principal


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
