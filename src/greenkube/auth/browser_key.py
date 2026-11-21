"""Signed browser cookies for API-key sign-in in ``API_AUTH_MODE=api_key`` (field report F-09).

A browser that signs in with an API key receives ``v1.<kid>.<exp>.<mac>``:
``kid`` is the scoped key id (``gk_<kid>_...``) or ``legacy`` for the bootstrap
key, ``exp`` a Unix time, and ``mac`` an HMAC-SHA256 over the rest. The key
itself never leaves the sign-in request. The signing key is derived from the
bootstrap key ``GREENKUBE_API_KEY``: rotating it signs every browser out. It is
never derived from the scrape token (mirrored to the ServiceMonitor namespace)
nor from stored key digests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from greenkube.auth.settings import AuthSettings

COOKIE_VERSION = "v1"
LEGACY_KID = "legacy"
#: Lifetime of a key sign-in without "keep me signed in"; with it, ``AUTH_SESSION_REMEMBER_LIFETIME``.
DEFAULT_LIFETIME = timedelta(hours=12)
_SIGNING_CONTEXT = b"greenkube-browser-cookie-v1"
_KID_RE = re.compile(r"^(legacy|[a-z0-9]{8})$")


@dataclass(frozen=True)
class BrowserKeyCookie:
    """A verified cookie: which key it names and when it expires."""

    kid: str
    expires_at: int


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def signing_key(settings: AuthSettings) -> bytes | None:
    """``HMAC(bootstrap key, "greenkube-browser-cookie-v1")``, or ``None`` without a bootstrap key."""
    if not settings.legacy_api_key:
        return None
    return hmac.new(settings.legacy_api_key.encode("utf-8"), _SIGNING_CONTEXT, hashlib.sha256).digest()


def _mac(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def issue_cookie(key: bytes, kid: str, *, now: datetime, lifetime: timedelta) -> tuple[str, int]:
    """Return ``(cookie value, expiry as Unix time)`` for the key ``kid``."""
    if not _KID_RE.match(kid):
        raise ValueError(f"Invalid key id {kid!r}.")
    expires_at = int((now + lifetime).timestamp())
    payload = f"{COOKIE_VERSION}.{kid}.{expires_at}"
    return f"{payload}.{_mac(key, payload)}", expires_at


def verify_cookie(key: bytes, value: str, *, now: datetime) -> BrowserKeyCookie | None:
    """Check the signature (constant time) and the expiry; ``None`` when either fails."""
    parts = (value or "").split(".")
    if len(parts) != 4:
        return None
    version, kid, expiry, mac = parts
    if version != COOKIE_VERSION or not _KID_RE.match(kid) or not expiry.isdigit():
        return None
    try:
        expected = _mac(key, f"{version}.{kid}.{expiry}")
    except UnicodeEncodeError:
        return None
    if not hmac.compare_digest(expected.encode("ascii"), mac.encode("utf-8", "replace")):
        return None
    if int(expiry) <= int(now.timestamp()):
        return None
    return BrowserKeyCookie(kid=kid, expires_at=int(expiry))


def csrf_token(key: bytes, cookie_value: str) -> str:
    """``HMAC(K, "csrf" | cookie)``: different for every cookie, useless without the signing key."""
    return _b64(hmac.new(key, b"csrf|" + cookie_value.encode("utf-8"), hashlib.sha256).digest())


def throttle_subject(token: str, key_id: str | None) -> str:
    """Rate-limit bucket of one attempt: the ``gk_`` key id, else a digest of the attempted key.

    A per-attempt subject keeps one client's typos from locking every other
    client behind the same IP out; the per-IP budget still bounds guessing.
    """
    if key_id:
        return f"api-key:{key_id}"
    return "api-key:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
