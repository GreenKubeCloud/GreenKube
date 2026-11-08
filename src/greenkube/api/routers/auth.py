"""Sign-in, sessions and scoped API keys (roadmap F14, F15).

``/api/v1/auth/login`` and ``/logout`` are reachable without credentials; every
other route here goes through the global :func:`authenticate` dependency, which
already enforced CSRF for session-authenticated mutations and the ``admin``
scope for API-key administration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from greenkube.api.dependencies import get_auth_service, get_principal
from greenkube.auth.bootstrap import erase_consumed_initial_password, initial_admin_secret_from_config
from greenkube.auth.http import clear_session_cookie, client_ip, is_same_origin, set_session_cookie
from greenkube.auth.metrics import record_auth_failure
from greenkube.auth.models import ApiKey, AuthSession
from greenkube.auth.principal import Principal, Scope
from greenkube.auth.service import AuthError, AuthService, IssuedSession
from greenkube.auth.settings import AuthSettings
from greenkube.auth.throttle import LoginThrottle
from greenkube.core.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

_NO_STORE = {"Cache-Control": "no-store"}


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=1024)
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
    service = _service(request)
    if not is_same_origin(request, settings, require=False):
        record_auth_failure("csrf")
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "Cross-site sign-in rejected."}
        )
    ip = client_ip(request, settings)
    throttle = _throttle(request, settings)
    retry_after = throttle.retry_after(ip, body.username)
    if retry_after:
        record_auth_failure("rate_limited")
        raise HTTPException(
            status_code=429,
            detail={"code": "TOO_MANY_ATTEMPTS", "message": "Too many sign-in attempts. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )
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
    """The signed-in user or API key; browser sessions also get their CSRF token."""
    response.headers.update(_NO_STORE)
    return _me(principal, _settings(), _current_session(request, principal))


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
    try:
        issued = await service.change_password(
            user,
            body.current_password,
            body.new_password,
            remember=remember,
            ip=client_ip(request, settings),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as error:
        if error.code == "INVALID_CREDENTIALS":
            record_auth_failure("bad_credentials")
        raise _error(error) from None
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
