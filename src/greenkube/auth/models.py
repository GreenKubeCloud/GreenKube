"""Stored authentication records (roadmap F14, F15)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class User:
    id: str
    username: str
    password_hash: str
    role: str
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    password_changed_at: datetime | None = None
    disabled_at: datetime | None = None
    # How the account signs in: "local" (password), "proxy" or "oidc" (F16).
    auth_source: str = "local"
    # Stable identity at the SSO provider ("proxy:<user>", "oidc:<subject>"); None for local accounts.
    external_id: str | None = None

    @property
    def is_local(self) -> bool:
        return self.auth_source == "local"


@dataclass(frozen=True)
class AuthSession:
    """A server-side session; only the SHA-256 of its cookie token is stored."""

    token_sha256: str
    id: str
    user_id: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    remember: bool = False
    user_agent: str | None = None
    ip: str | None = None
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.idle_expires_at and now < self.absolute_expires_at


@dataclass(frozen=True)
class ApiKey:
    """A scoped machine key; only the SHA-256 of its secret is stored."""

    id: str
    name: str
    prefix: str
    secret_sha256: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    created_by: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)


@dataclass(frozen=True)
class OidcLogin:
    """A pending OpenID Connect authorization request (F16), keyed by the SHA-256 of its ``state``."""

    state_sha256: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    return_to: str
    created_at: datetime
    expires_at: datetime
