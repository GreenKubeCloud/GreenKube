"""Identity from a trusted authentication proxy (roadmap F16, ``API_AUTH_MODE=proxy``).

oauth2-proxy, Authentik and Pomerium authenticate the user and forward the
request with identity headers (``X-Auth-Request-User`` / ``X-Forwarded-User``
and ``X-Auth-Request-Groups`` / ``X-Forwarded-Groups`` by default). GreenKube
accepts these headers **only** when the TCP peer is listed in
``TRUSTED_PROXY_IPS``; anything else could set them to any identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request

from greenkube.auth.http import from_trusted_proxy
from greenkube.auth.roles import split_groups_header
from greenkube.auth.settings import AuthSettings

MAX_PROXY_USERNAME_LENGTH = 128


@dataclass(frozen=True)
class ProxyIdentity:
    username: str
    groups: frozenset[str]

    @property
    def external_id(self) -> str:
        return f"proxy:{self.username}"


class UntrustedProxyHeader(Exception):
    """Identity headers arrived from a peer that is not a trusted proxy."""


def identity_headers_present(request: Request, settings: AuthSettings) -> bool:
    return any(request.headers.get(name) for name in settings.proxy_user_headers)


def _valid_username(value: str) -> bool:
    return 0 < len(value) <= MAX_PROXY_USERNAME_LENGTH and value.isprintable() and "," not in value


def read_proxy_identity(request: Request, settings: AuthSettings) -> ProxyIdentity | None:
    """Return the identity sent by a trusted proxy, ``None`` when no identity header is set.

    Raises :class:`UntrustedProxyHeader` when an identity header comes from any
    other peer, and ``ValueError`` when the header value is not a usable name.
    """
    if not identity_headers_present(request, settings):
        return None
    if not from_trusted_proxy(request, settings):
        raise UntrustedProxyHeader()
    username = ""
    for name in settings.proxy_user_headers:
        value = request.headers.get(name, "").strip()
        if value:
            username = value.lower()
            break
    if not _valid_username(username):
        raise ValueError("The identity header does not hold a valid user name.")
    groups: frozenset[str] = frozenset()
    for name in settings.proxy_groups_headers:
        groups |= split_groups_header(",".join(request.headers.getlist(name)))
    return ProxyIdentity(username=username, groups=groups)
