"""Identity from a trusted authentication proxy (roadmap F16, ``API_AUTH_MODE=proxy``).

oauth2-proxy, Authentik and Pomerium authenticate the user and forward the
request with identity headers (``X-Forwarded-User`` / ``X-Forwarded-Groups``
by default, as oauth2-proxy ``--pass-user-headers`` sets them). GreenKube
accepts these headers **only** when the TCP peer (not the ``X-Forwarded-For``
client) is listed in ``TRUSTED_PROXY_IPS``; anything else could set them to any
identity. Only one configured header family may be present on a request.
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


def header_families(settings: AuthSettings) -> list[tuple[str, str | None]]:
    """``(user header, groups header)`` pairs: the n-th groups header belongs to the n-th user header."""
    users = settings.proxy_user_headers
    groups = settings.proxy_groups_headers
    return [(user, groups[index] if index < len(groups) else None) for index, user in enumerate(users)]


def _present(request: Request, name: str | None) -> bool:
    return bool(name) and any(value.strip() for value in request.headers.getlist(name or ""))


def identity_headers_present(request: Request, settings: AuthSettings) -> bool:
    names = (*settings.proxy_user_headers, *settings.proxy_groups_headers)
    return any(_present(request, name) for name in names)


def _valid_username(value: str) -> bool:
    return 0 < len(value) <= MAX_PROXY_USERNAME_LENGTH and value.isprintable() and "," not in value


def read_proxy_identity(request: Request, settings: AuthSettings) -> ProxyIdentity | None:
    """Return the identity sent by a trusted proxy, ``None`` when no identity header is set.

    Raises :class:`UntrustedProxyHeader` when an identity or groups header comes
    from any other peer, and ``ValueError`` when the identity is unusable or
    ambiguous. Exactly one configured header family (user header and its paired
    groups header) may be present: a proxy overwrites only the family it
    manages, so a second family (or a repeated user header) is whatever the
    client chose to send and is refused rather than merged.
    """
    if not identity_headers_present(request, settings):
        return None
    if not from_trusted_proxy(request, settings):
        raise UntrustedProxyHeader()
    families = [
        (user, groups)
        for user, groups in header_families(settings)
        if _present(request, user) or _present(request, groups)
    ]
    extra_groups = settings.proxy_groups_headers[len(settings.proxy_user_headers) :]
    if len(families) != 1 or any(_present(request, name) for name in extra_groups):
        raise ValueError("The identity headers are missing or come from more than one header family.")
    user_header, groups_header = families[0]
    values = [value.strip() for value in request.headers.getlist(user_header) if value.strip()]
    if len(values) != 1:
        raise ValueError("The identity header is missing or repeated.")
    username = values[0].lower()
    if not _valid_username(username):
        raise ValueError("The identity header does not hold a valid user name.")
    groups: frozenset[str] = frozenset()
    if groups_header:
        groups = split_groups_header(",".join(request.headers.getlist(groups_header)))
    return ProxyIdentity(username=username, groups=groups)
