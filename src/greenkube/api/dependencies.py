# src/greenkube/api/dependencies.py
"""
FastAPI dependency injection functions.

These functions provide repository and service instances to API route handlers
via FastAPI's Depends() mechanism, keeping the API layer decoupled from
concrete implementations.
"""

import dataclasses
import hashlib
import logging
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Optional, Protocol, cast

from fastapi import Depends, HTTPException, Query, Request

from greenkube.auth import browser_key
from greenkube.auth.http import (
    CSRF_HEADER,
    cleared_session_cookie_header,
    client_ip,
    from_trusted_proxy,
    is_same_origin,
    peer_address,
)
from greenkube.auth.metrics import record_auth_failure
from greenkube.auth.principal import (
    ALL_SCOPES,
    ANONYMOUS,
    DEPRECATED_SCOPE_FALLBACKS,
    SAFE_METHODS,
    Principal,
    Role,
    Scope,
    required_scope,
)
from greenkube.auth.proxy import UntrustedProxyHeader, read_proxy_identity
from greenkube.auth.scrape import METRICS_SCRAPE_ACTOR
from greenkube.auth.service import AuthError, AuthService, api_key_principal, user_principal
from greenkube.auth.settings import AUTH_MODES, AuthSettings
from greenkube.auth.throttle import CredentialThrottle
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

    if getattr(cfg, "API_AUTH_MODE", "api_key") not in AUTH_MODES:
        raise HTTPException(status_code=503, detail="The configured authentication mode is not supported.")
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
_AUTH_PUBLIC_PATHS = (
    "/api/v1/auth/login",
    "/api/v1/auth/api-key-login",
    "/api/v1/auth/logout",
    "/api/v1/auth/providers",
    "/api/v1/auth/oidc/login",
    "/api/v1/auth/oidc/callback",
)
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


def _legacy_principal(token: str, access: str = "admin") -> Principal:
    """The deprecated bootstrap key: full admin, or ``metrics:read`` only with ``AUTH_LEGACY_API_KEY=metrics``."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    admin = access == "admin"
    return Principal(
        kind="apikey",
        name="legacy",
        role=Role.ADMIN.value if admin else None,
        scopes=ALL_SCOPES if admin else frozenset({Scope.METRICS_READ.value}),
        legacy=True,
        actor_override=f"api-key:{digest}",
    )


#: The dedicated Prometheus scrape token: ``metrics:read`` and nothing else.
METRICS_TOKEN_PRINCIPAL = Principal(
    kind="apikey",
    name="metrics-token",
    scopes=frozenset({Scope.METRICS_READ.value}),
    actor_override=METRICS_SCRAPE_ACTOR,
)


async def resolve_key_principal(token: str, settings: AuthSettings, service: AuthService | None) -> Principal | None:
    """Resolve an API key: the deprecated bootstrap key, the scrape token, then a scoped ``gk_`` key.

    Returns ``None`` when the token matches no active key.
    """
    token = (token or "").strip()
    if not token:
        return None
    # Compare digests so that neither the content nor the length of the key leaks through timing.
    if (
        settings.legacy_api_key
        and settings.legacy_api_key_access != "disabled"
        and constant_time_equals(sha256_hex(token), sha256_hex(settings.legacy_api_key))
    ):
        return _legacy_principal(token, settings.legacy_api_key_access)
    if settings.metrics_token and constant_time_equals(sha256_hex(token), sha256_hex(settings.metrics_token)):
        return METRICS_TOKEN_PRINCIPAL
    if service is not None and token.startswith("gk_"):
        key = await service.resolve_api_key(token)
        if key is not None:
            return api_key_principal(key)
    return None


async def _bearer_principal(request: Request, settings: AuthSettings, service: AuthService | None) -> Principal:
    """Resolve ``Authorization: Bearer``: the deprecated bootstrap key, the scrape token, then a scoped key."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        principal = await resolve_key_principal(token, settings, service)
        if principal is not None:
            return principal
    if await _open_access(settings, service):
        # Nothing to check a key against on an open development instance.
        return ANONYMOUS
    record_auth_failure("invalid_api_key")
    raise _bearer_unauthorized()


def browser_key_principal_for(principal: Principal, signing: bytes, cookie: str) -> Principal:
    """Mark a key principal as signed in through the browser cookie, with that cookie's CSRF token."""
    return dataclasses.replace(principal, browser_key=True, csrf_token=browser_key.csrf_token(signing, cookie))


async def browser_cookie_kid_principal(
    kid: str, settings: AuthSettings, service: AuthService | None
) -> Principal | None:
    """The current principal of the key a verified cookie names, or ``None`` if it no longer may sign in.

    ``legacy`` needs the bootstrap key still configured and not disabled (its
    current access applies); a scoped key is fetched again and must be active,
    so revocation and expiry sign the browser out. Either way, the ``read``
    scope is required to open the UI.
    """
    principal: Principal | None = None
    if kid == browser_key.LEGACY_KID:
        if settings.legacy_api_key and settings.legacy_api_key_access != "disabled":
            principal = _legacy_principal(settings.legacy_api_key, settings.legacy_api_key_access)
    elif service is not None:
        key = await service.active_api_key(kid)
        if key is not None:
            principal = api_key_principal(key)
    if principal is None or not principal.has_scope(Scope.READ.value):
        return None
    return principal


async def _browser_key_principal(
    request: Request, settings: AuthSettings, service: AuthService | None
) -> Principal | None:
    """In ``api_key`` mode, the signed cookie a browser got from ``/auth/api-key-login`` (F-09).

    A cookie that fails any check is marked for clearing in the 401 response.
    """
    if settings.mode != "api_key":
        return None
    cookie = request.cookies.get(settings.cookie_name)
    if not cookie:
        return None
    signing = browser_key.signing_key(settings)
    now = service.now() if service is not None else datetime.now(UTC)
    verified = browser_key.verify_cookie(signing, cookie, now=now) if signing else None
    principal = await browser_cookie_kid_principal(verified.kid, settings, service) if verified else None
    if principal is None:
        record_auth_failure("invalid_api_key")
        request.state.clear_auth_cookie = True
        return None
    return browser_key_principal_for(principal, signing or b"", cookie)


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
    request.state.auth_user = user
    return user_principal(user, session, token)


async def _proxy_principal(
    request: Request, settings: AuthSettings, service: AuthService | None, current: Principal | None
) -> Principal | None:
    """Resolve trusted authentication-proxy identity headers (``API_AUTH_MODE=proxy``, F16).

    Identity headers from a peer outside ``TRUSTED_PROXY_IPS`` are rejected
    outright: they are a spoofing attempt or a misconfigured proxy. A session
    of the same identity is kept (its role follows the forwarded groups); any
    other identity gets a principal without a session, which may read and
    which ``GET /auth/me`` turns into a session. Mutations need that session
    and its CSRF token. A local account that signed in with its password keeps
    its own session (break-glass access through the proxy).
    """
    try:
        identity = read_proxy_identity(request, settings)
    except UntrustedProxyHeader:
        record_auth_failure("untrusted_proxy_header")
        logger.warning(
            "Rejected identity headers from %s, which is not in TRUSTED_PROXY_IPS.",
            peer_address(request) or "unknown",
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNTRUSTED_PROXY_HEADER",
                "message": "Identity headers are only accepted from the trusted authentication proxy.",
            },
        ) from None
    except ValueError:
        record_auth_failure("sso")
        raise HTTPException(
            status_code=401, detail={"code": "INVALID_IDENTITY", "message": "The proxy sent no usable identity."}
        ) from None
    if identity is None:
        if (
            current is not None
            and current.is_session
            and getattr(getattr(request.state, "auth_user", None), "auth_source", None) == "proxy"
            and not from_trusted_proxy(request, settings)
        ):
            # A session the proxy started is only valid through the proxy, never around it.
            record_auth_failure("untrusted_proxy_header")
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "UNTRUSTED_PROXY_HEADER",
                    "message": "This session was started by the authentication proxy and is only valid through it.",
                },
            )
        return current
    if service is None:
        raise HTTPException(status_code=503, detail="The account store is not available.")
    user = getattr(request.state, "auth_user", None)
    if current is not None and current.is_session and getattr(user, "auth_source", None) == "local":
        # A local account signed in with its password (break-glass through the proxy) keeps its session.
        return current
    same_identity = (
        current is not None and current.is_session and getattr(user, "external_id", None) == (identity.external_id)
    )
    if same_identity and current is not None and current.role == service.sso_role(identity.groups):
        return current
    try:
        user = await service.sso_user("proxy", identity.username, identity.username, identity.groups)
    except AuthError as error:
        record_auth_failure("sso")
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code, "message": error.message}
        ) from None
    if same_identity and current is not None:
        # Same session, role re-synchronised from the forwarded groups.
        return dataclasses.replace(current, role=user.role, scopes=user_principal(user).scopes)
    request.state.auth_session = None
    request.state.pending_sso_user = user
    return user_principal(user)


async def _open_access(settings: AuthSettings, service: AuthService | None) -> bool:
    """Whether an unauthenticated request is served anonymously.

    Only a development instance with no bootstrap API key, no scrape token and
    no account is open, as before sessions existed. Production never is.
    """
    if settings.machine_credentials_configured or settings.environment == "production":
        return False
    if settings.mode == "api_key" or service is None:
        return True
    try:
        return not await service.users_exist()
    except Exception:
        logger.warning("Could not check whether accounts exist; refusing anonymous access.")
        return False


_SCRAPE_PATH = "/prometheus/metrics"


def _credential_throttle(request: Request, settings: AuthSettings) -> CredentialThrottle:
    """The per-process :class:`CredentialThrottle`, rebuilt when ``AUTH_LOGIN_RATE_LIMIT`` changes."""
    state = request.app.state
    current = getattr(state, "credential_throttle", None)
    if not isinstance(current, CredentialThrottle) or getattr(state, "credential_throttle_limit", None) != (
        settings.login_rate_limit
    ):
        current = CredentialThrottle(settings.login_rate_limit)
        state.credential_throttle = current
        state.credential_throttle_limit = settings.login_rate_limit
    return current


def _enforce_credential_throttle(
    request: Request, settings: AuthSettings, path: str
) -> tuple[CredentialThrottle, str, str]:
    """Refuse a machine credential with ``429`` while its client address has spent its failure budget (A.9).

    Prometheus scrapes (``/prometheus/metrics``) have their own bucket, so that
    failures on the API routes never stop a correct scrape. Returns what
    :meth:`CredentialThrottle.record_failure` needs if the credential fails.
    """
    throttle = _credential_throttle(request, settings)
    ip = client_ip(request, settings)
    bucket = "scrape" if path == _SCRAPE_PATH else "api"
    retry_after = throttle.retry_after(ip, bucket)
    if retry_after:
        record_auth_failure("rate_limited")
        raise HTTPException(
            status_code=429,
            detail={
                "code": "TOO_MANY_ATTEMPTS",
                "message": "Too many failed authentication attempts. Try again later.",
            },
            headers={"Retry-After": str(retry_after)},
        )
    return throttle, ip, bucket


def _remember(request: Request, principal: Principal) -> None:
    request.state.principal = principal
    request.state.authenticated_actor = principal.actor


async def authenticate(request: Request) -> None:
    """Resolve the request's :class:`Principal` and enforce scopes and CSRF (F14, F15).

    Order: a Bearer API key (the deprecated ``GREENKUBE_API_KEY`` bootstrap key,
    then scoped ``gk_`` keys), then the session cookie (in ``api_key`` mode, the
    cookie holding the API key the browser signed in with). Protected routes are
    ``/api/v1/*``, ``/api/v2/*`` and ``/prometheus/metrics``; the SPA, liveness, docs, OpenAPI
    schema and the provider webhook ingress route are public. Unsafe methods
    authenticated by a session cookie need a same-origin ``Origin``/``Referer``
    and an ``X-CSRF-Token`` header matching the session. Failed Bearer keys and
    API-key cookies are throttled per client address (:class:`CredentialThrottle`).
    """
    from greenkube.core.config import get_config

    path = _route_path(request)
    if not path.startswith(_PROTECTED_API_PREFIXES) and path != "/prometheus/metrics":
        return
    if path in _PUBLIC_API_PATHS or (request.method == "POST" and _is_webhook_ingress_path(path)):
        return

    settings = AuthSettings.from_config(get_config())
    if settings.mode not in AUTH_MODES:
        raise HTTPException(status_code=503, detail="The configured authentication mode is not supported.")
    service = get_auth_service(request)
    auth_public = path in _AUTH_PUBLIC_PATHS

    if request.headers.get("Authorization"):
        throttle, ip, bucket = _enforce_credential_throttle(request, settings, path)
        try:
            principal: Principal | None = await _bearer_principal(request, settings, service)
        except HTTPException as error:
            if error.status_code == 401:
                throttle.record_failure(ip, bucket)
            raise
    elif settings.mode == "api_key":
        if request.cookies.get(settings.cookie_name):
            throttle, ip, bucket = _enforce_credential_throttle(request, settings, path)
            principal = await _browser_key_principal(request, settings, service)
            if principal is None:
                throttle.record_failure(ip, bucket)
        else:
            principal = None
    else:
        principal = await _session_principal(request, settings, service)
        if settings.proxy_enabled and not auth_public:
            principal = await _proxy_principal(request, settings, service, principal)

    if principal is None:
        if auth_public:
            return
        if await _open_access(settings, service):
            principal = ANONYMOUS
        elif settings.environment == "production" and not settings.machine_credentials_configured and service is None:
            raise HTTPException(status_code=503, detail="API authentication is not configured.")
        else:
            headers = {"WWW-Authenticate": "Bearer"}
            if getattr(request.state, "clear_auth_cookie", False):
                headers["Set-Cookie"] = cleared_session_cookie_header(request, settings)
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED, headers=headers)

    _remember(request, principal)
    if auth_public:
        return

    if principal.must_change_password and path not in _PASSWORD_CHANGE_ALLOWED:
        raise _forbidden("PASSWORD_CHANGE_REQUIRED", "Change the initial password before using GreenKube.")

    scope = required_scope(request.method, path)
    if scope is not None and not principal.has_scope(scope):
        fallback = DEPRECATED_SCOPE_FALLBACKS.get(scope)
        if fallback is None or not principal.has_scope(fallback):
            record_auth_failure("forbidden_scope")
            raise _forbidden("INSUFFICIENT_SCOPE", f"This credential lacks the '{scope}' scope.")
        # Accepted for one minor release; the route answers with a Deprecation header.
        request.state.deprecated_scope = (fallback, scope)

    if (principal.kind == "user" or principal.browser_key) and request.method.upper() not in SAFE_METHODS:
        # A user principal without a session (proxy identity before GET /auth/me) has no
        # CSRF token: the same code makes the UI refresh /auth/me, which starts the session.
        # A browser signed in with an API key (cookie, ``api_key`` mode) is checked the same way.
        if (
            not (principal.is_session or principal.browser_key)
            or not is_same_origin(request, settings, require=True)
            or not constant_time_equals(request.headers.get(CSRF_HEADER), principal.csrf_token)
        ):
            record_auth_failure("csrf")
            raise _forbidden("CSRF_REJECTED", "Cross-site request rejected: reload the page and try again.")


def require_scope(scope: str | Scope):
    """Route dependency enforcing one scope, on top of the global :func:`authenticate` table (F16).

    Roles map to scopes (``viewer``: read; ``operator``: write, approve, deliver;
    ``admin``: everything), so ``require_scope(Scope.APPROVE)`` is "operator or
    above" for users and "an ``approve`` key" for machines. Declared on the
    mutating routes themselves so that a change to the path table can never
    silently open them.
    """
    needed = str(scope.value if isinstance(scope, Scope) else scope)

    def dependency(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            # Fail closed: a route guard never runs without the global authentication.
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
        if not principal.has_scope(needed):
            record_auth_failure("forbidden_scope")
            role = _ROLE_HINT.get(needed, "admin")
            raise _forbidden("INSUFFICIENT_SCOPE", f"This needs the '{needed}' scope (role {role} or above).")
        return principal

    dependency.__name__ = f"require_scope_{needed.replace(':', '_')}"
    return dependency


_ROLE_HINT = {
    Scope.READ.value: "viewer",
    Scope.METRICS_READ.value: "viewer",
    Scope.WRITE.value: "operator",
    Scope.APPROVE.value: "operator",
    Scope.PLANS_DELIVER.value: "operator",
}


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

    async def cancel_by_user(self, plan_id: str, *, actor: str = "system") -> Plan | None: ...


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
