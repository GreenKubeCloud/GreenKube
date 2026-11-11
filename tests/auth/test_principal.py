"""Scope table for API keys and roles (roadmap F15)."""

import pytest

from greenkube.auth.principal import ANONYMOUS, ROLE_SCOPES, Principal, normalize_scopes, required_scope
from greenkube.auth.tokens import csrf_token_for, new_api_key, parse_api_key


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("GET", "/prometheus/metrics", "metrics:read"),
        ("GET", "/api/v1/recommendations", "read"),
        ("POST", "/api/v1/optimization/runs", "write"),
        ("PATCH", "/api/v1/recommendations/1/ignore", "write"),
        ("POST", "/api/v1/automation/plans/abc/approve", "approve"),
        ("POST", "/api/v1/automation/plans/abc/delivery", "plans:deliver"),
        ("GET", "/api/v1/automation/plans/abc/patch", "read"),
        ("POST", "/api/v1/signals", "signals:write"),
        ("GET", "/api/v1/auth/api-keys", "admin"),
        ("POST", "/api/v1/auth/api-keys", "admin"),
        ("GET", "/api/v1/auth/me", None),
        ("DELETE", "/api/v1/auth/sessions/abc", None),
        ("POST", "/api/v1/auth/password", None),
    ],
)
def test_required_scope(method, path, scope):
    assert required_scope(method, path) == scope


def test_roles_and_admin_scope():
    viewer = Principal(kind="user", name="v", role="viewer", scopes=ROLE_SCOPES["viewer"])
    operator = Principal(kind="user", name="o", role="operator", scopes=ROLE_SCOPES["operator"])
    key = Principal(kind="apikey", name="k", scopes=frozenset({"admin"}))

    assert viewer.has_scope("read") and not viewer.has_scope("write")
    assert operator.has_scope("approve") and not operator.has_scope("admin")
    assert operator.has_scope("plans:deliver") and not viewer.has_scope("plans:deliver")
    assert key.has_scope("signals:write")
    assert viewer.actor == "user:v" and key.actor == "apikey:k" and ANONYMOUS.actor == "anonymous"


def test_scope_validation():
    assert normalize_scopes([" Read ", "metrics:read"]) == {"read", "metrics:read"}
    with pytest.raises(ValueError):
        normalize_scopes(["read", "root"])
    with pytest.raises(ValueError):
        normalize_scopes([])


def test_api_key_format_round_trips():
    key_id, secret, full = new_api_key()

    assert full == f"gk_{key_id}_{secret}"
    assert parse_api_key(full) == (key_id, secret)
    assert parse_api_key("gk_ABCDEFGH_" + secret) is None
    assert parse_api_key("Bearer " + full) is None


def test_csrf_token_is_derived_but_not_the_session_token():
    assert csrf_token_for("token-a") == csrf_token_for("token-a")
    assert csrf_token_for("token-a") != csrf_token_for("token-b")
    assert "token-a" not in csrf_token_for("token-a")
