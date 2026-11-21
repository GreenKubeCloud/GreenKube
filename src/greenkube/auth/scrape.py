"""Prometheus scrape credentials: the dedicated scrape token and the deprecated bootstrap key.

A Prometheus scrape needs read access to metrics only. Three credentials grant it:

* the **scrape token** ``GREENKUBE_METRICS_TOKEN`` (config ``METRICS_TOKEN``),
  generated once by the Helm credentials bootstrap hook and used by the chart's
  ServiceMonitors. It is accepted by the API's ``/prometheus/metrics`` and by
  the controller and worker metrics listeners, and nowhere else: on any other
  API route it is a principal with the ``metrics:read`` scope only;
* a scoped API key with the ``metrics:read`` scope (API only: the listeners do
  not read the account store);
* the deprecated bootstrap key ``GREENKUBE_API_KEY``, unless
  ``AUTH_LEGACY_API_KEY=disabled``. On the controller and worker listeners it is
  accepted only while no scrape token is configured: the chart projects the
  scrape token into the Pods exactly when its ServiceMonitors send it, so the
  admin key never needs to reach a metrics listener once the token exists.

Failed attempts on the listeners feed a per-peer
:class:`~greenkube.auth.throttle.CredentialThrottle` (roadmap A.9), so the
bootstrap key cannot be guessed there at full speed either.

``AUTH_LEGACY_API_KEY`` decides what the bootstrap key may do: ``admin`` (the
historical full-admin credential, the only credential of ``API_AUTH_MODE=api_key``),
``metrics`` (scrape only) or ``disabled`` (rejected everywhere).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from greenkube.auth.metrics import record_auth_failure
from greenkube.auth.peer import transport_peer
from greenkube.auth.throttle import CredentialThrottle
from greenkube.auth.tokens import constant_time_equals, sha256_hex

logger = logging.getLogger(__name__)

#: What the deprecated ``GREENKUBE_API_KEY`` may do.
LEGACY_API_KEY_ACCESS = ("admin", "metrics", "disabled")
METRICS_SCRAPE_ACTOR = "metrics-token"

_INVALID = "Invalid or missing scrape token."


def legacy_api_key_access(cfg: Any) -> str:
    value = str(getattr(cfg, "AUTH_LEGACY_API_KEY", "admin") or "admin").strip().lower()
    return value if value in LEGACY_API_KEY_ACCESS else "disabled"


def scrape_tokens(cfg: Any) -> tuple[str, ...]:
    """The bearer tokens a metrics listener accepts (empty: none configured).

    The scrape token when one is configured, otherwise the bootstrap key unless it is disabled.
    """
    metrics_token = str(getattr(cfg, "METRICS_TOKEN", "") or "").strip()
    if metrics_token:
        return (metrics_token,)
    legacy = str(getattr(cfg, "API_KEY", "") or "")
    if legacy and legacy_api_key_access(cfg) != "disabled":
        return (legacy,)
    return ()


def matches(token: str, expected: str) -> bool:
    """Constant-time comparison of digests: neither the content nor the length leaks."""
    return bool(token) and bool(expected) and constant_time_equals(sha256_hex(token), sha256_hex(expected))


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _listener_throttle(request: Request, cfg: Any) -> CredentialThrottle:
    """The listener's per-process :class:`CredentialThrottle`, rebuilt when ``AUTH_LOGIN_RATE_LIMIT`` changes."""
    limit = str(getattr(cfg, "AUTH_LOGIN_RATE_LIMIT", "5/minute") or "5/minute")
    state = request.app.state
    current = getattr(state, "scrape_throttle", None)
    if not isinstance(current, CredentialThrottle) or getattr(state, "scrape_throttle_limit", None) != limit:
        current = CredentialThrottle(limit)
        state.scrape_throttle = current
        state.scrape_throttle_limit = limit
    return current


def verify_scrape_request(request: Request, *, require_configured: bool) -> None:
    """Accept the scrape token or (without one, unless disabled) the bootstrap key as ``Authorization: Bearer``.

    Failed attempts count per TCP peer (the listeners trust no proxy header);
    past the budget every attempt from that peer answers ``429`` with
    ``Retry-After`` until the window frees up.

    With no credential configured, a development instance (and the controller,
    ``require_configured=False``, whose endpoint the chart kept on the pod network
    without credentials before the scrape token existed) serves the metrics
    openly; a production worker fails closed with 503.
    """
    from greenkube.core.config import get_config

    cfg = get_config()
    tokens = scrape_tokens(cfg)
    if not tokens:
        if require_configured and str(getattr(cfg, "ENVIRONMENT", "development")).lower() == "production":
            raise HTTPException(status_code=503, detail="Metrics authentication is not configured.")
        return
    throttle = _listener_throttle(request, cfg)
    peer = transport_peer(request.scope, request.client.host if request.client else "") or "unknown"
    retry_after = throttle.retry_after(peer, "scrape")
    if retry_after:
        record_auth_failure("key_rate_limited")
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    token = bearer_token(request)
    if not any(matches(token, expected) for expected in tokens):
        throttle.record_failure(peer, "scrape")
        record_auth_failure("invalid_api_key")
        raise HTTPException(status_code=401, detail=_INVALID, headers={"WWW-Authenticate": "Bearer"})
    request.state.authenticated_actor = METRICS_SCRAPE_ACTOR
