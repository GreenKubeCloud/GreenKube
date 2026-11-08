"""Authentication settings resolved from :class:`greenkube.core.config.Config`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


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

    @property
    def sessions_enabled(self) -> bool:
        return self.mode == "session"

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
        )
