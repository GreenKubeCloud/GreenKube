"""Authentication settings resolved from :class:`greenkube.core.config.Config`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from greenkube.auth.roles import normalize_default_role, parse_role_groups

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

#: Modes with browser sessions; ``api_key`` serves Bearer API keys only.
SESSION_MODES = frozenset({"session", "oidc", "proxy"})
AUTH_MODES = frozenset({"api_key", *SESSION_MODES})
DEFAULT_PROXY_USER_HEADERS = ("X-Auth-Request-User", "X-Forwarded-User")
DEFAULT_PROXY_GROUPS_HEADERS = ("X-Auth-Request-Groups", "X-Forwarded-Groups")


def split_header_names(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    names = tuple(item.strip() for item in str(value or "").split(",") if item.strip())
    return names or default


def _role_groups(value: Any) -> dict[str, frozenset[str]]:
    try:
        return parse_role_groups(value)
    except ValueError:
        # Config validation rejects this at startup; never grant roles from a malformed mapping.
        return {}


def _default_role(value: Any) -> str:
    try:
        return normalize_default_role(value)
    except ValueError:
        return "none"


def parse_auth_duration(value: str, default: timedelta) -> timedelta:
    """Parse ``30s``, ``15m``, ``12h`` or ``7d`` (``m`` is minutes)."""
    match = re.fullmatch(r"(\d+)([smhd])", str(value or "").strip().lower())
    if not match or int(match.group(1)) <= 0:
        return default
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


@dataclass(frozen=True)
class AuthSettings:
    mode: str = "session"
    environment: str = "development"
    cookie_name: str = "greenkube_session"
    cookie_secure: str = "auto"
    idle_timeout: timedelta = timedelta(hours=12)
    absolute_lifetime: timedelta = timedelta(days=7)
    remember_lifetime: timedelta = timedelta(days=30)
    login_rate_limit: str = "5/minute"
    admin_username: str = "admin"
    legacy_api_key: str = ""
    trusted_proxy_ips: str = ""
    cors_origins: str = ""
    # --- single sign-on and roles (F16) ---
    local_login: bool = True
    proxy_user_headers: tuple[str, ...] = DEFAULT_PROXY_USER_HEADERS
    proxy_groups_headers: tuple[str, ...] = DEFAULT_PROXY_GROUPS_HEADERS
    role_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    default_role: str = "viewer"
    external_url: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_redirect_url: str = ""
    oidc_provider_name: str = "SSO"

    @property
    def sessions_enabled(self) -> bool:
        """Browser sessions exist in every mode except the machine-only ``api_key`` mode."""
        return self.mode in SESSION_MODES

    @property
    def password_login_enabled(self) -> bool:
        """Local accounts sign in with a password in ``session`` mode, and in SSO modes unless disabled."""
        return self.sessions_enabled and (self.mode == "session" or self.local_login)

    @property
    def proxy_enabled(self) -> bool:
        return self.mode == "proxy"

    @property
    def oidc_enabled(self) -> bool:
        return self.mode == "oidc" and bool(self.oidc_issuer and self.oidc_client_id)

    @classmethod
    def from_config(cls, cfg: Any) -> AuthSettings:
        return cls(
            mode=str(getattr(cfg, "API_AUTH_MODE", "session") or "session").lower(),
            environment=str(getattr(cfg, "ENVIRONMENT", "development") or "development").lower(),
            cookie_name=str(getattr(cfg, "SESSION_COOKIE_NAME", "") or "greenkube_session"),
            cookie_secure=str(getattr(cfg, "SESSION_COOKIE_SECURE", "auto") or "auto").lower(),
            idle_timeout=parse_auth_duration(getattr(cfg, "AUTH_SESSION_IDLE_TIMEOUT", "12h"), timedelta(hours=12)),
            absolute_lifetime=parse_auth_duration(
                getattr(cfg, "AUTH_SESSION_ABSOLUTE_LIFETIME", "7d"), timedelta(days=7)
            ),
            remember_lifetime=parse_auth_duration(
                getattr(cfg, "AUTH_SESSION_REMEMBER_LIFETIME", "30d"), timedelta(days=30)
            ),
            login_rate_limit=str(getattr(cfg, "AUTH_LOGIN_RATE_LIMIT", "5/minute") or "5/minute"),
            admin_username=str(getattr(cfg, "AUTH_ADMIN_USERNAME", "admin") or "admin").strip().lower(),
            legacy_api_key=str(getattr(cfg, "API_KEY", "") or ""),
            trusted_proxy_ips=str(getattr(cfg, "TRUSTED_PROXY_IPS", "") or ""),
            cors_origins=str(getattr(cfg, "CORS_ORIGINS", "") or ""),
            local_login=bool(getattr(cfg, "AUTH_LOCAL_LOGIN", True)),
            proxy_user_headers=split_header_names(
                getattr(cfg, "AUTH_PROXY_USER_HEADERS", ""), DEFAULT_PROXY_USER_HEADERS
            ),
            proxy_groups_headers=split_header_names(
                getattr(cfg, "AUTH_PROXY_GROUPS_HEADERS", ""), DEFAULT_PROXY_GROUPS_HEADERS
            ),
            role_groups=_role_groups(getattr(cfg, "AUTH_ROLE_GROUPS", "")),
            default_role=_default_role(getattr(cfg, "AUTH_DEFAULT_ROLE", "viewer")),
            external_url=str(getattr(cfg, "GREENKUBE_EXTERNAL_URL", "") or "").rstrip("/"),
            oidc_issuer=str(getattr(cfg, "OIDC_ISSUER", "") or "").strip(),
            oidc_client_id=str(getattr(cfg, "OIDC_AUDIENCE", "") or "").strip(),
            oidc_client_secret=str(getattr(cfg, "OIDC_CLIENT_SECRET", "") or "").strip(),
            oidc_scopes=str(getattr(cfg, "OIDC_SCOPES", "") or "openid profile email"),
            oidc_username_claim=str(getattr(cfg, "OIDC_USERNAME_CLAIM", "") or "preferred_username"),
            oidc_groups_claim=str(getattr(cfg, "OIDC_GROUPS_CLAIM", "") or "groups"),
            oidc_redirect_url=str(getattr(cfg, "OIDC_REDIRECT_URL", "") or "").strip(),
            oidc_provider_name=str(getattr(cfg, "OIDC_PROVIDER_NAME", "") or "SSO").strip()[:64] or "SSO",
        )
