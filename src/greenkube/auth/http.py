"""HTTP details of browser sessions: client address, cookies and same-origin checks."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response

from greenkube.auth.peer import transport_peer
from greenkube.auth.settings import AuthSettings

CSRF_HEADER = "X-CSRF-Token"


def _networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_trusted(host: str, networks) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def peer_address(request: Request) -> str:
    """The TCP peer of the request, before any ``X-Forwarded-For`` rewrite."""
    return transport_peer(request.scope, request.client.host if request.client else "")


def from_trusted_proxy(request: Request, settings: AuthSettings) -> bool:
    """Whether the connection itself comes from ``TRUSTED_PROXY_IPS`` (never the forwarded client)."""
    return _is_trusted(peer_address(request), _networks(settings.trusted_proxy_ips))


def client_ip(request: Request, settings: AuthSettings) -> str:
    """The client address, honouring ``X-Forwarded-For`` only from ``TRUSTED_PROXY_IPS``.

    The header is read from the right: proxies append the address they received
    the request from, so the rightmost entry that is not itself a trusted proxy is
    the real client. Entries further left are whatever the client chose to send
    and cannot be trusted (they would let one client spread its sign-in attempts
    over many per-IP budgets).
    """
    host = peer_address(request) or "unknown"
    networks = _networks(settings.trusted_proxy_ips)
    if not _is_trusted(host, networks):
        return host
    hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, networks):
            return hop
    return hops[0] if hops else host


def is_https(request: Request, settings: AuthSettings) -> bool:
    """Whether the browser reached GreenKube over HTTPS (directly or through a trusted proxy)."""
    if request.url.scheme == "https":
        return True
    if from_trusted_proxy(request, settings):
        proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        return proto == "https"
    return False


def cookie_secure(request: Request, settings: AuthSettings) -> bool:
    if settings.cookie_secure == "always":
        return True
    if settings.cookie_secure == "never":
        return False
    return is_https(request, settings)


def cookie_path(request: Request) -> str:
    root_path = request.scope.get("root_path") or ""
    return root_path.rstrip("/") + "/" if isinstance(root_path, str) and root_path else "/"


def set_session_cookie(
    response: Response,
    request: Request,
    settings: AuthSettings,
    token: str,
    *,
    remember: bool,
    expires_at: datetime,
    now: datetime,
) -> None:
    """``HttpOnly; SameSite=Strict; Path=/``; ``Secure`` per ``SESSION_COOKIE_SECURE``.

    Without "keep me signed in" the cookie is a browser-session cookie; with it,
    it lasts as long as the server-side absolute lifetime.
    """
    max_age = max(0, int((expires_at - now).total_seconds())) if remember else None
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=max_age,
        path=cookie_path(request),
        secure=cookie_secure(request, settings),
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, request: Request, settings: AuthSettings) -> None:
    response.delete_cookie(
        settings.cookie_name,
        path=cookie_path(request),
        secure=cookie_secure(request, settings),
        httponly=True,
        samesite="strict",
    )


def cleared_session_cookie_header(request: Request, settings: AuthSettings) -> str:
    """The ``Set-Cookie`` header value that clears the auth cookie, for error responses."""
    response = Response()
    clear_session_cookie(response, request, settings)
    return response.headers["set-cookie"]


_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def is_loopback_host(request: Request) -> bool:
    """Whether the browser addressed GreenKube by a loopback name or address (``kubectl port-forward``)."""
    host = request.url.hostname or ""
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_of(value: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return parts.scheme.lower(), parts.netloc.lower()


def _normalize_netloc(netloc: str) -> str:
    """Lower-case a host[:port], dropping an explicit default port."""
    netloc = netloc.strip().lower()
    for suffix in (":80", ":443"):
        if netloc.endswith(suffix):
            return netloc[: -len(suffix)]
    return netloc


def _expected_hosts(request: Request, settings: AuthSettings) -> set[str]:
    hosts = set()
    host_header = request.headers.get("host", "")
    if host_header:
        hosts.add(_normalize_netloc(host_header))
    if from_trusted_proxy(request, settings):
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
        if forwarded_host:
            hosts.add(_normalize_netloc(forwarded_host))
    return hosts


def is_same_origin(request: Request, settings: AuthSettings, *, require: bool) -> bool:
    """Check ``Origin`` (or ``Referer``) against the request's own host.

    Explicit, credentialed ``CORS_ORIGINS`` entries are also accepted; a
    wildcard never is. With ``require``, a request carrying neither header is
    rejected (browsers always send ``Origin`` on unsafe fetches).
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return not require
    origin = _origin_of(source)
    if origin is None:
        return False
    scheme, netloc = origin
    if _normalize_netloc(netloc) in _expected_hosts(request, settings):
        return True
    allowed = {item.strip().rstrip("/").lower() for item in settings.cors_origins.split(",") if item.strip()}
    allowed.discard("*")
    return f"{scheme}://{netloc}" in allowed
