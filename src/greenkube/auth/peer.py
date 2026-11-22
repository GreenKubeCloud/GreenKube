"""The TCP peer of a request, kept apart from the client that proxy headers describe.

``greenkube-api`` honours ``X-Forwarded-For`` / ``X-Forwarded-Proto`` from
``TRUSTED_PROXY_IPS`` (uvicorn's :class:`ProxyHeadersMiddleware`), which
rewrites ``scope["client"]`` to the end user's address. Deciding whether a
request *comes from* the trusted proxy (identity headers, ``X-Forwarded-Host``)
must look at the connection itself, not at that rewritten client: otherwise a
legitimate proxied request is judged by the end user's address, and an end
user whose address falls inside a trusted range passes for the proxy.

:class:`TransportPeerMiddleware` records the connection's address under
:data:`TRANSPORT_PEER_SCOPE_KEY` *before* the rewrite.
"""

from __future__ import annotations

from typing import Any

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

TRANSPORT_PEER_SCOPE_KEY = "greenkube.transport_peer"


class TransportPeerMiddleware:
    """Record the TCP peer, then apply the proxy headers of trusted peers (pure ASGI)."""

    def __init__(self, app: Any, trusted_proxy_ips: str = "") -> None:
        self.app = app
        trusted = ",".join(item.strip() for item in (trusted_proxy_ips or "").split(",") if item.strip())
        self._inner = ProxyHeadersMiddleware(app, trusted_hosts=trusted) if trusted else app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client")
            scope[TRANSPORT_PEER_SCOPE_KEY] = client[0] if client else ""
        await self._inner(scope, receive, send)


def transport_peer(scope: Any, client_host: str | None) -> str:
    """The connection's address: the recorded peer, else the (unrewritten) ASGI client."""
    recorded = scope.get(TRANSPORT_PEER_SCOPE_KEY) if hasattr(scope, "get") else None
    if isinstance(recorded, str):
        return recorded
    return client_host or ""
