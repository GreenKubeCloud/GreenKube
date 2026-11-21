"""Sign-in, sessions, SSO, users and scoped API keys (roadmap F14, F15, F16).

``/api/v1/auth/login``, ``/api-key-login``, ``/logout``, ``/providers`` and the OpenID Connect
``/oidc/login`` and ``/oidc/callback`` routes are reachable without
credentials; every other route here goes through the global
:func:`authenticate` dependency, which already enforced CSRF for
session-authenticated mutations and the ``admin`` scope for user and API-key
administration.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from greenkube.api.dependencies import (
    browser_key_principal_for,
    get_auth_service,
    get_principal,
    resolve_key_principal,
)
from greenkube.auth import browser_key, oidc
from greenkube.auth.bootstrap import erase_consumed_initial_password, initial_admin_secret_from_config
from greenkube.auth.http import (
    clear_session_cookie,
    client_ip,
    cookie_path,
    cookie_secure,
    from_trusted_proxy,
    is_https,
    is_loopback_host,
    is_same_origin,
    set_session_cookie,
)
from greenkube.auth.metrics import record_auth_failure
from greenkube.auth.models import ApiKey, AuthSession, OidcLogin, User
from greenkube.auth.passwords import validate_new_password
from greenkube.auth.principal import Principal, Scope
from greenkube.auth.service import AuthError, AuthService, IssuedSession
from greenkube.auth.settings import AuthSettings
from greenkube.auth.throttle import LoginThrottle
from greenkube.auth.tokens import constant_time_equals, parse_api_key, sha256_hex
from greenkube.core.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

_NO_STORE = {"Cache-Control": "no-store"}


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=1024)
    remember: bool = False


class ApiKeyLoginRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=1024)
    remember: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=1, max_length=1024)


class SessionInfo(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    remember: bool
    user_agent: str | None = None
    ip: str | None = None
    current: bool = False


class MeResponse(BaseModel):
    """The signed-in principal. ``csrf_token`` is set for browser sessions only."""

    authenticated: bool
    kind: Literal["user", "apikey", "anonymous"]
    name: str
    actor: str
    role: str | None = None
    scopes: list[str]
    must_change_password: bool = False
    csrf_token: str | None = None
    auth_mode: str
    session: SessionInfo | None = None
    deprecated_credential: bool = False
    auth_source: str | None = Field(
        default=None, description="How the user signed in: local (password), proxy or oidc (F16)."
    )


class PasswordChangeResponse(MeResponse):
    initial_admin_secret_erased: bool = False


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[str] = Field(..., min_length=1, max_length=10)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyInfo(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_by: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    active: bool


class ApiKeyCreated(ApiKeyInfo):
    key: str = Field(..., description="The full key. It is shown once and cannot be retrieved again.")


def _settings() -> AuthSettings:
    return AuthSettings.from_config(get_config())


def _service(request: Request) -> AuthService:
    service = get_auth_service(request)
    if service is None:
        raise HTTPException(status_code=503, detail="The account store is not available.")
    return service


def _throttle(request: Request, settings: AuthSettings) -> LoginThrottle:
    current = getattr(request.app.state, "login_throttle", None)
    if not isinstance(current, LoginThrottle) or getattr(request.app.state, "login_throttle_limit", None) != (
        settings.login_rate_limit
    ):
        current = LoginThrottle(settings.login_rate_limit)
        request.app.state.login_throttle = current
        request.app.state.login_throttle_limit = settings.login_rate_limit
    return current


def _enforce_throttle(throttle: LoginThrottle, ip: str, username: str) -> None:
    retry_after = throttle.retry_after(ip, username)
    if retry_after:
        record_auth_failure("rate_limited")
        raise HTTPException(
            status_code=429,
            detail={"code": "TOO_MANY_ATTEMPTS", "message": "Too many sign-in attempts. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )


def _error(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})


def _session_info(session: AuthSession, current_id: str | None) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        idle_expires_at=session.idle_expires_at,
        absolute_expires_at=session.absolute_expires_at,
        remember=session.remember,
        user_agent=session.user_agent,
        ip=session.ip,
        current=session.id == current_id,
    )


def _me(principal: Principal, settings: AuthSettings, session: AuthSession | None = None) -> MeResponse:
    return MeResponse(
        authenticated=principal.kind != "anonymous",
        kind=principal.kind,  # type: ignore[arg-type]
        name=principal.name,
        actor=principal.actor,
        role=principal.role,
        scopes=sorted(principal.scopes),
        must_change_password=principal.must_change_password,
        csrf_token=principal.csrf_token,
        auth_mode=settings.mode,
        session=_session_info(session, session.id) if session else None,
        deprecated_credential=principal.legacy,
        auth_source=principal.auth_source,
    )


def _issued_me(issued: IssuedSession, settings: AuthSettings) -> MeResponse:
    from greenkube.auth.service import user_principal

    return _me(user_principal(issued.user, issued.session, issued.token), settings, issued.session)


def _current_session(request: Request, principal: Principal) -> AuthSession | None:
    session = getattr(request.state, "auth_session", None)
    if principal.is_session and isinstance(session, AuthSession):
        return session
    return None


def _require_user(principal: Principal) -> Principal:
    if principal.kind != "user" or not principal.user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "SESSION_REQUIRED", "message": "This operation needs a signed-in user."},
        )
    return principal


def _api_key_info(key: ApiKey, now: datetime) -> ApiKeyInfo:
    return ApiKeyInfo(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        scopes=sorted(key.scopes),
        created_by=key.created_by,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
        active=key.is_active(now),
    )


@router.post("/login", response_model=MeResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    """Sign in with a username and password and receive a new session cookie."""
    settings = _settings()
    if not settings.sessions_enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "SESSIONS_DISABLED", "message": "Browser sign-in is disabled (API_AUTH_MODE=api_key)."},
        )
    if not settings.password_login_enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PASSWORD_LOGIN_DISABLED",
                "message": "Sign in with single sign-on (AUTH_LOCAL_LOGIN=false).",
            },
        )
    service = _service(request)
    if not is_same_origin(request, settings, require=False):
        record_auth_failure("csrf")
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "Cross-site sign-in rejected."}
        )
    ip = client_ip(request, settings)
    throttle = _throttle(request, settings)
    _enforce_throttle(throttle, ip, body.username)
    try:
        issued = await service.login(
            body.username,
            body.password,
            remember=body.remember,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
            previous_token=request.cookies.get(settings.cookie_name),
        )
    except AuthError as error:
        throttle.record_failure(ip, body.username)
        record_auth_failure("bad_credentials")
        raise _error(error) from None
    throttle.record_success(ip, body.username)
    set_session_cookie(
        response,
        request,
        settings,
        issued.token,
        remember=body.remember,
        expires_at=issued.session.absolute_expires_at,
        now=service.now(),
    )
    response.headers.update(_NO_STORE)
    return _issued_me(issued, settings)


@router.post("/api-key-login", response_model=MeResponse)
async def api_key_login(body: ApiKeyLoginRequest, request: Request, response: Response):
    """Sign a browser in with an API key in ``API_AUTH_MODE=api_key`` (field report F-09).

    The key is checked like a Bearer key and exchanged for a signed, expiring
    cookie ``v1.<kid>.<exp>.<mac>`` (HttpOnly, ``SameSite=Strict``) that never
    contains the key. It lasts 12 hours, or ``AUTH_SESSION_REMEMBER_LIFETIME``
    with "keep me signed in". Every request checks the signature, the expiry
    and that the key is still active. The signing key derives from the
    bootstrap key ``GREENKUBE_API_KEY``: without one this answers 409, and
    rotating it signs every browser out. Over plain HTTP only a loopback host
    (``kubectl port-forward``) is accepted. Other modes answer 409.
    """
    settings = _settings()
    if settings.mode != "api_key":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "API_KEY_LOGIN_DISABLED",
                "message": "Sign in with a username and password or single sign-on (API_AUTH_MODE is not api_key).",
            },
        )
    signing = browser_key.signing_key(settings)
    if signing is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "API_KEY_LOGIN_UNAVAILABLE",
                "message": "Browser sign-in with an API key needs the bootstrap key GREENKUBE_API_KEY, "
                "which signs the browser cookie. Configure it or switch to API_AUTH_MODE=session.",
            },
        )
    if not is_same_origin(request, settings, require=False):
        record_auth_failure("csrf")
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "Cross-site sign-in rejected."}
        )
    https = is_https(request, settings)
    if not https and not is_loopback_host(request):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "HTTPS_REQUIRED",
                "message": "Signing in with an API key needs HTTPS (or localhost through kubectl port-forward): "
                "over plain HTTP the key and the cookie could be read on the network.",
            },
        )
    key = body.key.strip()
    parsed = parse_api_key(key)
    subject = browser_key.throttle_subject(key, parsed[0] if parsed else None)
    ip = client_ip(request, settings)
    throttle = _throttle(request, settings)
    _enforce_throttle(throttle, ip, subject)
    service = get_auth_service(request)
    principal = await resolve_key_principal(key, settings, service)
    kid = browser_key.LEGACY_KID if principal is not None and principal.legacy else None
    if principal is not None and kid is None:
        kid = principal.api_key_id
    if principal is None:
        throttle.record_failure(ip, subject)
        record_auth_failure("invalid_api_key")
        raise HTTPException(status_code=401, detail={"code": "INVALID_API_KEY", "message": "Invalid API key."})
    throttle.record_success(ip, subject)
    if kid is None or not principal.has_scope(Scope.READ.value):
        record_auth_failure("forbidden_scope")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "INSUFFICIENT_SCOPE",
                "message": "This key cannot open the UI: it needs the 'read' scope (scrape tokens never can).",
            },
        )
    now = service.now() if service is not None else datetime.now(UTC)
    lifetime = settings.remember_lifetime if body.remember else browser_key.DEFAULT_LIFETIME
    cookie, _expires_at = browser_key.issue_cookie(signing, kid, now=now, lifetime=lifetime)
    response.set_cookie(
        settings.cookie_name,
        cookie,
        max_age=int(lifetime.total_seconds()) if body.remember else None,
        path=cookie_path(request),
        secure=True if https else cookie_secure(request, settings),
        httponly=True,
        samesite="strict",
    )
    response.headers.update(_NO_STORE)
    logger.info("A browser signed in with API key %s.", principal.actor)
    return _me(browser_key_principal_for(principal, signing, cookie), settings)


@router.post("/logout", status_code=204)
async def logout(request: Request):
    """Revoke the current session server-side and clear the cookie."""
    settings = _settings()
    response = Response(status_code=204)
    token = request.cookies.get(settings.cookie_name)
    if token:
        if not is_same_origin(request, settings, require=False):
            raise HTTPException(
                status_code=403, detail={"code": "CSRF_REJECTED", "message": "Cross-site sign-out rejected."}
            )
        service = get_auth_service(request)
        if service is not None:
            await service.logout(token)
    clear_session_cookie(response, request, settings)
    return response


@router.get("/me", response_model=MeResponse)
async def me(request: Request, response: Response, principal: Principal = Depends(get_principal)):
    """The signed-in user or API key; browser sessions also get their CSRF token.

    Behind a trusted authentication proxy (``API_AUTH_MODE=proxy``, F16), the
    first call for an identity starts its server-side session.
    """
    response.headers.update(_NO_STORE)
    settings = _settings()
    pending = getattr(request.state, "pending_sso_user", None)
    if isinstance(pending, User) and settings.sessions_enabled:
        service = _service(request)
        try:
            issued = await service.issue_session(
                pending,
                ip=client_ip(request, settings),
                user_agent=request.headers.get("user-agent"),
                previous_token=request.cookies.get(settings.cookie_name),
            )
        except AuthError as error:
            raise _error(error) from None
        set_session_cookie(
            response,
            request,
            settings,
            issued.token,
            remember=False,
            expires_at=issued.session.absolute_expires_at,
            now=service.now(),
        )
        logger.info("Started a %s session for %s.", pending.auth_source, pending.username)
        return _issued_me(issued, settings)
    return _me(principal, settings, _current_session(request, principal))


@router.post("/password", response_model=PasswordChangeResponse)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
):
    """Change the password; every session of the user is revoked and a new one is issued."""
    principal = _require_user(principal)
    settings = _settings()
    service = _service(request)
    user = await service.repository.get_user(principal.user_id or "")
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    current = _current_session(request, principal)
    remember = bool(current and current.remember)
    # Wrong current passwords count as failed sign-ins for this user, so a stolen
    # session cookie does not allow unthrottled guessing of the password.
    ip = client_ip(request, settings)
    throttle = _throttle(request, settings)
    _enforce_throttle(throttle, ip, user.username)
    try:
        issued = await service.change_password(
            user,
            body.current_password,
            body.new_password,
            remember=remember,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as error:
        if error.code == "INVALID_CREDENTIALS":
            throttle.record_failure(ip, user.username)
            record_auth_failure("bad_credentials")
        raise _error(error) from None
    throttle.record_success(ip, user.username)
    erased = False
    if user.must_change_password:
        erased = await erase_consumed_initial_password(service, initial_admin_secret_from_config(get_config()))
    set_session_cookie(
        response,
        request,
        settings,
        issued.token,
        remember=remember,
        expires_at=issued.session.absolute_expires_at,
        now=service.now(),
    )
    response.headers.update(_NO_STORE)
    payload = _issued_me(issued, settings).model_dump()
    return PasswordChangeResponse(**payload, initial_admin_secret_erased=erased)


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions(request: Request, principal: Principal = Depends(get_principal)):
    """Active sessions of the signed-in user."""
    principal = _require_user(principal)
    sessions = await _service(request).list_sessions(principal.user_id or "")
    return [_session_info(session, principal.session_id) for session in sessions]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, request: Request, principal: Principal = Depends(get_principal)):
    """Sign out one of the user's sessions (the current one clears the cookie)."""
    principal = _require_user(principal)
    if not await _service(request).revoke_session(session_id, user_id=principal.user_id or ""):
        raise HTTPException(status_code=404, detail="Session not found.")
    response = Response(status_code=204)
    if session_id == principal.session_id:
        clear_session_cookie(response, request, _settings())
    return response


def _require_admin(principal: Principal) -> Principal:
    if not principal.has_scope(Scope.ADMIN.value):
        raise HTTPException(
            status_code=403, detail={"code": "INSUFFICIENT_SCOPE", "message": "This needs the 'admin' scope."}
        )
    return principal


@router.get("/api-keys", response_model=list[ApiKeyInfo])
async def list_api_keys(request: Request, principal: Principal = Depends(get_principal)):
    """Every API key, active or not. Secrets are never returned."""
    _require_admin(principal)
    service = _service(request)
    now = service.now()
    return [_api_key_info(key, now) for key in await service.list_api_keys()]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest, request: Request, response: Response, principal: Principal = Depends(get_principal)
):
    """Create a scoped key. The response is the only time the full key is shown."""
    _require_admin(principal)
    service = _service(request)
    expires_in = timedelta(days=body.expires_in_days) if body.expires_in_days else None
    try:
        created = await service.create_api_key(
            body.name, body.scopes, created_by=principal.actor, expires_in=expires_in
        )
    except AuthError as error:
        raise _error(error) from None
    logger.info("API key %s (%s) created by %s.", created.key.prefix, created.key.name, principal.actor)
    response.headers.update(_NO_STORE)
    info = _api_key_info(created.key, service.now())
    return ApiKeyCreated(**info.model_dump(), key=created.plaintext)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, request: Request, principal: Principal = Depends(get_principal)):
    """Revoke a key immediately."""
    _require_admin(principal)
    if not await _service(request).revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found or already revoked.")
    logger.info("API key gk_%s revoked by %s.", key_id, principal.actor)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Sign-in options and single sign-on (F16)                                     #
# --------------------------------------------------------------------------- #


class SsoProvider(BaseModel):
    kind: Literal["oidc", "proxy"]
    name: str
    login_path: str | None = Field(
        default=None, description="Path, relative to the API base, that starts the sign-in (OIDC only)."
    )


class AuthProviders(BaseModel):
    """What the sign-in page offers. Public: it reveals no account."""

    mode: str
    password: bool
    api_key: bool = Field(
        default=False, description="The form asks for an API key (API_AUTH_MODE=api_key, field report F-09)."
    )
    sso: SsoProvider | None = None


@router.get("/providers", response_model=AuthProviders)
async def providers(response: Response):
    """Sign-in options of this instance: password form, SSO button, or both."""
    settings = _settings()
    response.headers.update(_NO_STORE)
    sso: SsoProvider | None = None
    if settings.oidc_enabled:
        sso = SsoProvider(kind="oidc", name=settings.oidc_provider_name, login_path="/auth/oidc/login")
    elif settings.proxy_enabled:
        sso = SsoProvider(kind="proxy", name="your authentication proxy")
    return AuthProviders(
        mode=settings.mode, password=settings.password_login_enabled, api_key=settings.mode == "api_key", sso=sso
    )


def _oidc_cookie_name(settings: AuthSettings) -> str:
    return f"{settings.cookie_name}_oidc"


def _app_root(request: Request) -> str:
    """The UI root path (``ROOT_PATH`` aware), ending with ``/``."""
    return cookie_path(request)


def _oidc_cookie_path(request: Request) -> str:
    return f"{_app_root(request)}api/v1/auth/oidc/"


def _redirect_uri(request: Request, settings: AuthSettings) -> str:
    """``OIDC_REDIRECT_URL``, else ``GREENKUBE_EXTERNAL_URL`` + callback, else the request's own origin."""
    if settings.oidc_redirect_url:
        return settings.oidc_redirect_url
    if settings.external_url:
        return f"{settings.external_url}/{oidc.CALLBACK_PATH}"
    scheme = "https" if is_https(request, settings) else request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    if from_trusted_proxy(request, settings):
        host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip() or host
    return f"{scheme}://{host}{_app_root(request)}{oidc.CALLBACK_PATH}"


def _sso_failure(request: Request, settings: AuthSettings, code: str, detail: str = "") -> RedirectResponse:
    """Back to the sign-in page with a stable error code; the detail goes to the log only."""
    record_auth_failure("sso")
    logger.warning("Single sign-on failed: %s (%s).", code, detail or code)
    response = RedirectResponse(f"{_app_root(request)}?sso_error={code}", status_code=303)
    response.headers.update(_NO_STORE)
    response.delete_cookie(_oidc_cookie_name(settings), path=_oidc_cookie_path(request))
    return response


def _oidc_disabled() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"code": "OIDC_DISABLED", "message": "OpenID Connect is not configured."}
    )


@router.get("/oidc/login")
async def oidc_login(request: Request, return_to: str = Query(default="/", max_length=512)):
    """Start an OpenID Connect sign-in: redirect to the provider (authorization code + PKCE)."""
    settings = _settings()
    if not settings.oidc_enabled:
        raise _oidc_disabled()
    service = _service(request)
    client = oidc.get_oidc_client(request.app.state, settings)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = oidc.new_code_verifier()
    redirect_uri = _redirect_uri(request, settings)
    try:
        location = await client.authorization_url(
            state=state, nonce=nonce, verifier=verifier, redirect_uri=redirect_uri
        )
    except oidc.OidcError as error:
        return _sso_failure(request, settings, error.code, error.detail)
    now = service.now()
    await service.repository.create_oidc_login(
        OidcLogin(
            state_sha256=sha256_hex(state),
            nonce=nonce,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            return_to=oidc.safe_return_to(return_to),
            created_at=now,
            expires_at=now + oidc.LOGIN_TTL,
        )
    )
    response = RedirectResponse(location, status_code=302)
    response.headers.update(_NO_STORE)
    # Binds the state to this browser. SameSite=Lax: the provider's redirect back is a
    # cross-site top-level navigation, on which Strict cookies are not sent.
    response.set_cookie(
        _oidc_cookie_name(settings),
        state,
        max_age=int(oidc.LOGIN_TTL.total_seconds()),
        path=_oidc_cookie_path(request),
        secure=cookie_secure(request, settings),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=512),
    error: str | None = Query(default=None, max_length=256),
):
    """Finish an OpenID Connect sign-in and start a session (single use, ten-minute window)."""
    settings = _settings()
    if not settings.oidc_enabled:
        raise _oidc_disabled()
    service = _service(request)
    if error:
        return _sso_failure(request, settings, "denied", f"the provider answered {error[:64]!r}")
    if not code or not state:
        return _sso_failure(request, settings, "invalid_request", "code or state missing")
    if not constant_time_equals(request.cookies.get(_oidc_cookie_name(settings)), state):
        return _sso_failure(request, settings, "state_mismatch", "the state does not belong to this browser")
    pending = await service.repository.consume_oidc_login(sha256_hex(state))
    if pending is None or pending.expires_at <= service.now():
        return _sso_failure(request, settings, "state_expired", "unknown, used or expired state")
    client = oidc.get_oidc_client(request.app.state, settings)
    try:
        tokens = await client.exchange_code(
            code=code, verifier=pending.code_verifier, redirect_uri=pending.redirect_uri
        )
        claims = await client.verify_id_token(tokens["id_token"], nonce=pending.nonce)
    except oidc.OidcError as failure:
        return _sso_failure(request, settings, failure.code, failure.detail)
    identity = oidc.identity_from_claims(claims, settings)
    try:
        user = await service.sso_user("oidc", identity.subject, identity.username, identity.groups)
        issued = await service.issue_session(
            user,
            ip=client_ip(request, settings),
            user_agent=request.headers.get("user-agent"),
            previous_token=request.cookies.get(settings.cookie_name),
        )
    except AuthError as refused:
        return _sso_failure(request, settings, refused.code.lower(), refused.message)
    target = f"{_app_root(request).rstrip('/')}{oidc.safe_return_to(pending.return_to)}"
    response = RedirectResponse(target, status_code=303)
    response.headers.update(_NO_STORE)
    response.delete_cookie(_oidc_cookie_name(settings), path=_oidc_cookie_path(request))
    set_session_cookie(
        response,
        request,
        settings,
        issued.token,
        remember=False,
        expires_at=issued.session.absolute_expires_at,
        now=service.now(),
    )
    logger.info("Signed in %s through OpenID Connect (role %s).", user.username, user.role)
    return response


# --------------------------------------------------------------------------- #
# Users and roles (admin, F16)                                                 #
# --------------------------------------------------------------------------- #


RoleName = Literal["viewer", "operator", "admin"]


class UserInfo(BaseModel):
    id: str
    username: str
    role: str
    auth_source: str
    disabled: bool
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    role_managed_by_groups: bool = Field(description="SSO accounts get their role from the group mapping at sign-in.")


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024, description="Initial password, changed at first sign-in.")
    role: RoleName = "viewer"


class UserUpdateRequest(BaseModel):
    role: RoleName | None = None
    disabled: bool | None = None


def _user_info(user: User) -> UserInfo:
    return UserInfo(
        id=user.id,
        username=user.username,
        role=user.role,
        auth_source=user.auth_source,
        disabled=user.disabled_at is not None,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        updated_at=user.updated_at,
        role_managed_by_groups=not user.is_local,
    )


@router.get("/users", response_model=list[UserInfo])
async def list_users(request: Request, principal: Principal = Depends(get_principal)):
    """Every account with its role and sign-in source (``admin``)."""
    _require_admin(principal)
    return [_user_info(user) for user in await _service(request).list_users()]


@router.post("/users", response_model=UserInfo, status_code=201)
async def create_user(body: UserCreateRequest, request: Request, principal: Principal = Depends(get_principal)):
    """Create a local account; it chooses a new password at its first sign-in (``admin``)."""
    _require_admin(principal)
    reason = validate_new_password(body.password, username=body.username)
    if reason:
        raise HTTPException(status_code=422, detail={"code": "WEAK_PASSWORD", "message": reason})
    try:
        user = await _service(request).create_user(
            body.username, body.password, role=body.role, must_change_password=True
        )
    except AuthError as error:
        raise _error(error) from None
    logger.info("User %s (%s) created by %s.", user.username, user.role, principal.actor)
    return _user_info(user)


@router.patch("/users/{user_id}", response_model=UserInfo)
async def update_user(
    user_id: str, body: UserUpdateRequest, request: Request, principal: Principal = Depends(get_principal)
):
    """Change a role or disable an account (``admin``); the last active admin cannot be demoted or disabled.

    The role of an SSO account is recomputed from its groups at its next sign-in.
    """
    _require_admin(principal)
    service = _service(request)
    try:
        user = await service.repository.get_user(user_id)
        if user is None:
            raise AuthError("USER_NOT_FOUND", "User not found.", status_code=404)
        if body.role is not None:
            user = await service.set_user_role(user_id, body.role)
        if body.disabled is not None:
            user = await service.set_user_disabled(user_id, body.disabled)
    except AuthError as error:
        raise _error(error) from None
    logger.info(
        "User %s updated by %s (role=%s, disabled=%s).", user.username, principal.actor, body.role, body.disabled
    )
    return _user_info(user)
