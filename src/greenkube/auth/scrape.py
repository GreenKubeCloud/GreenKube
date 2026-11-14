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
  ``AUTH_LEGACY_API_KEY=disabled``. Installs upgraded from a chart that scraped
  with it keep working until the ServiceMonitors switch to the scrape token.

``AUTH_LEGACY_API_KEY`` decides what the bootstrap key may do: ``admin`` (the
historical full-admin credential, the only credential of ``API_AUTH_MODE=api_key``),
``metrics`` (scrape only) or ``disabled`` (rejected everywhere).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

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
    """The bearer tokens a metrics listener accepts (empty: none configured)."""
    tokens = []
    metrics_token = str(getattr(cfg, "METRICS_TOKEN", "") or "").strip()
    if metrics_token:
        tokens.append(metrics_token)
    legacy = str(getattr(cfg, "API_KEY", "") or "")
    if legacy and legacy_api_key_access(cfg) != "disabled":
        tokens.append(legacy)
    return tuple(tokens)


def matches(token: str, expected: str) -> bool:
    """Constant-time comparison of digests: neither the content nor the length leaks."""
    return bool(token) and bool(expected) and constant_time_equals(sha256_hex(token), sha256_hex(expected))


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def verify_scrape_request(request: Request, *, require_configured: bool) -> None:
    """Accept the scrape token or (unless disabled) the bootstrap key as ``Authorization: Bearer``.

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
    token = bearer_token(request)
    if not any(matches(token, expected) for expected in tokens):
        raise HTTPException(status_code=401, detail=_INVALID, headers={"WWW-Authenticate": "Bearer"})
    request.state.authenticated_actor = METRICS_SCRAPE_ACTOR
