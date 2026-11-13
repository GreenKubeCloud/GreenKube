"""Authenticated principals, roles and API-key scopes (roadmap F14, F15).

A :class:`Principal` is what every authenticated request resolves to: a signed-in
user (session cookie), a scoped API key, the deprecated bootstrap key
``GREENKUBE_API_KEY``, or the anonymous principal of an unsecured development
instance. Its :attr:`Principal.actor` string is the audit identity recorded in
events, plan approvals and ledger entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Scope(StrEnum):
    """Permissions carried by API keys and implied by user roles."""

    READ = "read"
    WRITE = "write"
    APPROVE = "approve"
    SIGNALS_WRITE = "signals:write"
    PLANS_DELIVER = "plans:deliver"
    METRICS_READ = "metrics:read"
    ADMIN = "admin"


ALL_SCOPES: frozenset[str] = frozenset(scope.value for scope in Scope)


class Role(StrEnum):
    """User roles (F16 maps SSO groups onto them)."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


ROLE_SCOPES: dict[str, frozenset[str]] = {
    Role.VIEWER.value: frozenset({Scope.READ.value, Scope.METRICS_READ.value}),
    Role.OPERATOR.value: frozenset(
        {Scope.READ.value, Scope.WRITE.value, Scope.APPROVE.value, Scope.PLANS_DELIVER.value, Scope.METRICS_READ.value}
    ),
    Role.ADMIN.value: ALL_SCOPES,
}


@dataclass(frozen=True)
class Principal:
    """The identity behind one request.

    ``kind`` is ``user``, ``apikey`` or ``anonymous``. ``legacy`` marks the
    deprecated ``GREENKUBE_API_KEY`` bootstrap key, whose audit actor keeps the
    historical ``api-key:<digest>`` form.
    """

    kind: str
    name: str
    role: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None
    csrf_token: str | None = None
    must_change_password: bool = False
    user_id: str | None = None
    api_key_id: str | None = None
    legacy: bool = False
    actor_override: str | None = None

    @property
    def actor(self) -> str:
        """Audit actor: ``user:<name>``, ``apikey:<name>``, ``api-key:<digest>`` or ``anonymous``."""
        if self.actor_override:
            return self.actor_override
        if self.kind == "anonymous":
            return "anonymous"
        return f"{self.kind}:{self.name}"

    @property
    def is_session(self) -> bool:
        return self.kind == "user" and self.session_id is not None

    def has_scope(self, scope: str) -> bool:
        return Scope.ADMIN.value in self.scopes or scope in self.scopes


ANONYMOUS = Principal(kind="anonymous", name="anonymous", role=Role.ADMIN.value, scopes=ALL_SCOPES)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Mutations that approve a plan and queue its pull request.
_APPROVE_PATHS = (re.compile(r"^/api/v1/automation/plans/[^/]+/approve$"),)
# Reports that a plan was delivered outside GreenKube (``patch`` and ``ci`` modes, roadmap F4):
# a CI job holds ``read`` + ``plans:deliver``, never ``write`` or ``approve``.
_DELIVERY_PATHS = (re.compile(r"^/api/v1/automation/plans/[^/]+/delivery$"),)
# Connector reports pushed like signals (KRR, Polaris: roadmap G2, G4).
_REPORT_PATHS = frozenset({"/api/v1/sources/krr/reports", "/api/v1/sources/polaris/reports"})
_ADMIN_PREFIXES = ("/api/v1/auth/api-keys", "/api/v1/auth/users", "/api/v1/automation/repositories/allowlist")
# Endpoints any authenticated principal may call for itself.
SELF_SERVICE_PATHS = frozenset({"/api/v1/auth/me"})
_SELF_SERVICE_PREFIXES = ("/api/v1/auth/sessions", "/api/v1/auth/password", "/api/v1/auth/logout")


def required_scope(method: str, path: str) -> str | None:
    """Return the scope a request needs, or ``None`` when authentication suffices.

    The table is coarse on purpose: reads need ``read``, mutations ``write``,
    plan approvals ``approve``, delivery reports ``plans:deliver``, signal pushes ``signals:write``, the Prometheus
    endpoint ``metrics:read``, and user or API-key administration ``admin``.
    """
    method = method.upper()
    if path == "/prometheus/metrics":
        return Scope.METRICS_READ.value
    if path in SELF_SERVICE_PATHS or path.startswith(_SELF_SERVICE_PREFIXES):
        return None
    if path.startswith(_ADMIN_PREFIXES):
        return Scope.ADMIN.value
    if (path.startswith("/api/v1/signals") or path in _REPORT_PATHS) and method not in SAFE_METHODS:
        return Scope.SIGNALS_WRITE.value
    if method in SAFE_METHODS:
        return Scope.READ.value
    if any(pattern.match(path) for pattern in _APPROVE_PATHS):
        return Scope.APPROVE.value
    if any(pattern.match(path) for pattern in _DELIVERY_PATHS):
        return Scope.PLANS_DELIVER.value
    return Scope.WRITE.value


def normalize_scopes(scopes: list[str] | tuple[str, ...] | set[str] | frozenset[str]) -> frozenset[str]:
    """Validate scope names, raising ``ValueError`` on an unknown one."""
    normalized = frozenset(str(scope).strip().lower() for scope in scopes if str(scope).strip())
    unknown = sorted(normalized - ALL_SCOPES)
    if unknown:
        raise ValueError(f"Unknown scope(s): {', '.join(unknown)}.")
    if not normalized:
        raise ValueError("At least one scope is required.")
    return normalized
