"""Opaque session tokens, CSRF tokens and API-key formats (roadmap F14, F15)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string

SESSION_TOKEN_BYTES = 32  # 256-bit opaque cookie value
API_KEY_SECRET_BYTES = 32
API_KEY_ID_LENGTH = 8
API_KEY_PREFIX = "gk_"
_ID_ALPHABET = string.ascii_lowercase + string.digits
_API_KEY_RE = re.compile(r"^gk_([a-z0-9]{8})_([A-Za-z0-9_-]{20,128})$")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """Return a new opaque, URL-safe 256-bit session token."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def new_public_id(length: int = 16) -> str:
    """Return a random identifier safe to show in the UI (never the token)."""
    return secrets.token_hex(length // 2)


def csrf_token_for(session_token: str) -> str:
    """Derive the session's CSRF token from its (HttpOnly) session token.

    The browser script cannot read the cookie, so it learns this value only
    from ``GET /auth/me`` and ``POST /auth/login`` on the same origin. Deriving
    it avoids storing a second secret per session.
    """
    digest = hashlib.sha256(b"greenkube-csrf:" + session_token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def constant_time_equals(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_api_key() -> tuple[str, str, str]:
    """Return ``(key_id, secret, full_key)`` for a new ``gk_<id>_<secret>`` key."""
    key_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(API_KEY_ID_LENGTH))
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    return key_id, secret, f"{API_KEY_PREFIX}{key_id}_{secret}"


def parse_api_key(token: str) -> tuple[str, str] | None:
    """Split a ``gk_<id>_<secret>`` key into ``(id, secret)``; ``None`` if malformed."""
    match = _API_KEY_RE.match(token or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def api_key_prefix(key_id: str) -> str:
    """The non-secret part shown in listings."""
    return f"{API_KEY_PREFIX}{key_id}"
