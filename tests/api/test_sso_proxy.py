"""Single sign-on through a trusted authentication proxy, and enforced roles (roadmap F16).

Identity headers (``X-Forwarded-User`` / ``X-Forwarded-Groups`` by default)
are honoured only when the TCP peer is in ``TRUSTED_PROXY_IPS``; the first
``GET /auth/me`` of an identity starts the same server-side session as a
password sign-in.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.api.dependencies import get_automation_plan_service
from greenkube.auth.metrics import AUTH_FAILURES
from tests.api.auth_support import (  # pyrefly: ignore[missing-import]
    ADMIN_PASSWORD,
    ORIGIN,
    VIEWER_PASSWORD,
    auth_harness,
)

PROXY = ("10.0.0.5", 41000)
OUTSIDER = ("203.0.113.9", 41000)
ROLE_GROUPS = json.dumps({"admin": ["platform-admins"], "operator": ["sre", "oncall"]})
PROXY_ENV = {
    "API_AUTH_MODE": "proxy",
    "TRUSTED_PROXY_IPS": "10.0.0.0/24",
    "AUTH_ROLE_GROUPS": ROLE_GROUPS,
}
APPROVE = "/api/v1/automation/plans/plan-1/approve"
APPROVE_BODY = {"digest": "sha256:" + "a" * 64}


def _headers(user: str, groups: str = "", *, header: str = "X-Forwarded-User") -> dict[str, str]:
    headers = {header: user}
    if groups:
        headers["X-Forwarded-Groups"] = groups
    return headers


def _failures(reason: str) -> float:
    return AUTH_FAILURES.labels(reason=reason)._value.get()


def _unavailable_plan_service():
    return None


# --------------------------------------------------------------------------- #
# Trusted headers only                                                         #
# --------------------------------------------------------------------------- #


def test_identity_headers_from_an_untrusted_address_are_rejected(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=OUTSIDER, **PROXY_ENV) as h:
        before = _failures("untrusted_proxy_header")
        for headers in (_headers("admin", "platform-admins"), {"X-Forwarded-Groups": "platform-admins"}):
            response = h.client.get("/api/v1/auth/me", headers=headers)
            assert response.status_code == 401, response.text
            assert response.json()["detail"]["code"] == "UNTRUSTED_PROXY_HEADER"
            assert "set-cookie" not in response.headers

        # The spoofed identity never reaches the account store.
        users = h.client.portal.call(h.service.list_users)
        assert {user.username for user in users} == {"admin", "viewer"}
        assert _failures("untrusted_proxy_header") == before + 2


def test_spoofed_headers_cannot_ride_on_a_real_session(tmp_path, monkeypatch):
    """A signed-in local viewer adding identity headers from an untrusted address is rejected, not upgraded."""
    with auth_harness(tmp_path, monkeypatch, client_address=OUTSIDER, **PROXY_ENV) as h:
        assert h.login("viewer", VIEWER_PASSWORD).status_code == 200
        response = h.client.get("/api/v1/auth/me", headers=_headers("boss", "platform-admins"))
        assert response.status_code == 401
        assert h.client.get("/api/v1/auth/me").json()["role"] == "viewer"


def test_trusted_proxy_identity_starts_a_session_with_the_mapped_role(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        headers = _headers("Alice", "staff,sre")
        me = h.client.get("/api/v1/auth/me", headers=headers)

        assert me.status_code == 200, me.text
        body = me.json()
        assert body["name"] == "alice" and body["actor"] == "user:alice"
        assert body["role"] == "operator" and body["auth_source"] == "proxy"
        assert body["csrf_token"] and body["session"]
        cookie = me.headers["set-cookie"]
        assert "greenkube_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie

        # Next calls reuse the session: no new cookie, same CSRF token.
        again = h.client.get("/api/v1/auth/me", headers=headers)
        assert "set-cookie" not in again.headers
        assert again.json()["csrf_token"] == body["csrf_token"]

        sessions = h.client.get("/api/v1/auth/sessions", headers=headers).json()
        assert len(sessions) == 1 and sessions[0]["current"]


def test_highest_mapped_role_wins_and_the_default_applies_without_a_group(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        boss = h.client.get("/api/v1/auth/me", headers=_headers("boss", "sre, platform-admins"))
        assert boss.json()["role"] == "admin"
        h.client.cookies.clear()
        guest = h.client.get("/api/v1/auth/me", headers=_headers("guest", "marketing"))
        assert guest.json()["role"] == "viewer"


def test_default_role_none_refuses_users_without_a_mapped_group(tmp_path, monkeypatch):
    env = {**PROXY_ENV, "AUTH_DEFAULT_ROLE": "none"}
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **env) as h:
        refused = h.client.get("/api/v1/auth/me", headers=_headers("guest", "marketing"))
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "NO_ROLE"
        assert "set-cookie" not in refused.headers


def test_role_follows_the_forwarded_groups_on_the_same_session(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        first = h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).json()
        assert first["role"] == "operator"

        demoted = h.client.get("/api/v1/auth/me", headers=_headers("alice", "staff")).json()
        assert demoted["role"] == "viewer"
        assert demoted["session"]["id"] == first["session"]["id"]
        h.client.app.dependency_overrides[get_automation_plan_service] = _unavailable_plan_service
        forbidden = h.client.post(
            APPROVE,
            json=APPROVE_BODY,
            headers={**_headers("alice", "staff"), "Origin": ORIGIN, "X-CSRF-Token": demoted["csrf_token"]},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_a_header_identity_must_start_its_session_before_mutating(tmp_path, monkeypatch):
    """Without a session there is no CSRF token: the UI refreshes /auth/me (CSRF_REJECTED) and retries."""
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        h.client.app.dependency_overrides[get_automation_plan_service] = _unavailable_plan_service
        headers = {**_headers("alice", "sre"), "Origin": ORIGIN}

        # Reads work at once.
        assert h.client.get("/api/v1/auth/sessions", headers=headers).status_code == 200
        blocked = h.client.post(APPROVE, json=APPROVE_BODY, headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "CSRF_REJECTED"

        csrf = h.client.get("/api/v1/auth/me", headers=headers).json()["csrf_token"]
        allowed = h.client.post(APPROVE, json=APPROVE_BODY, headers={**headers, "X-CSRF-Token": csrf})
        assert allowed.status_code == 503  # past authentication and the operator check: no plan service here


def test_another_identity_replaces_an_sso_session(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        alice = h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).json()
        bob = h.client.get("/api/v1/auth/me", headers=_headers("bob"))
        assert bob.json()["name"] == "bob"
        assert bob.json()["session"]["id"] != alice["session"]["id"]
        # Alice's session was revoked when Bob's started in the same browser.
        alice_user = h.client.portal.call(h.service.repository.get_user_by_external_id, "proxy:alice")
        assert h.client.portal.call(h.service.list_sessions, alice_user.id) == []


def test_an_sso_identity_never_takes_over_a_local_account(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        conflict = h.client.get("/api/v1/auth/me", headers=_headers("admin", "platform-admins"))
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "ACCOUNT_CONFLICT"


def test_local_accounts_keep_password_sign_in_in_proxy_mode(tmp_path, monkeypatch):
    """Break-glass: the local admin signs in with its password, even through the proxy."""
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        signed_in = h.login(headers=_headers("alice", "sre"))
        assert signed_in.status_code == 200, signed_in.text
        me = h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).json()
        assert me["name"] == "admin" and me["auth_source"] == "local"


def test_sso_accounts_have_no_password(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre"))
        h.client.cookies.clear()
        for password in ("!sso", "", "anything-long-enough"):
            response = h.login("alice", password or "x")
            assert response.status_code == 401

        csrf = h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).json()["csrf_token"]
        change = h.client.post(
            "/api/v1/auth/password",
            json={"current_password": "x", "new_password": "a-brand-new-password"},
            headers={**_headers("alice", "sre"), "Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert change.status_code == 409
        assert change.json()["detail"]["code"] == "PASSWORD_MANAGED_BY_SSO"


def test_disabled_sso_accounts_are_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre"))
        alice = h.client.portal.call(h.service.repository.get_user_by_external_id, "proxy:alice")
        h.client.portal.call(h.service.set_user_disabled, alice.id, True)
        refused = h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre"))
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "ACCOUNT_DISABLED"


def test_custom_identity_headers(tmp_path, monkeypatch):
    env = {
        **PROXY_ENV,
        "AUTH_PROXY_USER_HEADERS": "X-Authentik-Username",
        "AUTH_PROXY_GROUPS_HEADERS": "X-Authentik-Groups",
    }
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **env) as h:
        me = h.client.get(
            "/api/v1/auth/me", headers={"X-Authentik-Username": "carol", "X-Authentik-Groups": "platform-admins"}
        )
        assert me.json()["role"] == "admin"
        # The default header names are not identity headers any more.
        h.client.cookies.clear()
        assert h.client.get("/api/v1/auth/me", headers=_headers("dave")).status_code == 401


def test_providers_describe_the_sign_in_options(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        body = h.client.get("/api/v1/auth/providers").json()
        assert body == {
            "mode": "proxy",
            "password": True,
            "sso": {"kind": "proxy", "name": "your authentication proxy", "login_path": None},
        }
    env = {**PROXY_ENV, "AUTH_LOCAL_LOGIN": "false"}
    (tmp_path / "nolocal").mkdir()
    with auth_harness(tmp_path / "nolocal", monkeypatch, client_address=PROXY, **env) as h:
        assert h.client.get("/api/v1/auth/providers").json()["password"] is False
        refused = h.login()
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "PASSWORD_LOGIN_DISABLED"


def test_session_mode_ignores_identity_headers(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, TRUSTED_PROXY_IPS="10.0.0.0/24") as h:
        assert h.client.get("/api/v1/auth/me", headers=_headers("alice", "platform-admins")).status_code == 401


# --------------------------------------------------------------------------- #
# Configuration fails closed                                                   #
# --------------------------------------------------------------------------- #


def test_proxy_mode_requires_trusted_proxy_addresses():
    from greenkube.core.config import Config

    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        Config(API_AUTH_MODE="proxy", TRUSTED_PROXY_IPS="")
    assert Config(API_AUTH_MODE="proxy", TRUSTED_PROXY_IPS="10.0.0.0/8").API_AUTH_MODE == "proxy"


@pytest.mark.parametrize(
    "mapping",
    ['{"superuser": ["x"]}', "admin=platform", '["admin"]', '{"admin": [1]}'],
)
def test_malformed_role_mappings_are_rejected(mapping):
    from greenkube.core.config import Config

    with pytest.raises(ValueError, match="AUTH_ROLE_GROUPS"):
        Config(AUTH_ROLE_GROUPS=mapping)


def test_unknown_default_role_is_rejected():
    from greenkube.core.config import Config

    with pytest.raises(ValueError, match="AUTH_DEFAULT_ROLE"):
        Config(AUTH_DEFAULT_ROLE="root")


# --------------------------------------------------------------------------- #
# Roles enforced on the mutating routes                                        #
# --------------------------------------------------------------------------- #


def test_viewer_gets_403_on_plan_approval_and_operator_passes(tmp_path, monkeypatch):
    users = [
        ("admin", ADMIN_PASSWORD, "admin", False),
        ("viewer", VIEWER_PASSWORD, "viewer", False),
        ("operator", "operator-password-1", "operator", False),
    ]
    with auth_harness(tmp_path, monkeypatch, users=users) as h:
        service = MagicMock()
        service.approve_plan = AsyncMock(side_effect=AssertionError("a viewer must never reach approval"))
        h.client.app.dependency_overrides[get_automation_plan_service] = lambda: None

        h.login("viewer", VIEWER_PASSWORD)
        forbidden = h.client.post(APPROVE, json=APPROVE_BODY, headers=h.csrf())
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"

        h.client.cookies.clear()
        h.login("operator", "operator-password-1")
        allowed = h.client.post(APPROVE, json=APPROVE_BODY, headers=h.csrf())
        assert allowed.status_code == 503, allowed.text  # authorized; no plan service in this test app


def test_route_guards_hold_even_if_the_global_scope_table_misses_a_route(tmp_path, monkeypatch):
    """Defense in depth: each mutating route declares its own scope (F16)."""
    import greenkube.api.dependencies as dependencies

    monkeypatch.setattr(dependencies, "required_scope", lambda method, path: None)
    with auth_harness(tmp_path, monkeypatch) as h:
        h.client.app.dependency_overrides[get_automation_plan_service] = lambda: None
        h.login("viewer", VIEWER_PASSWORD)
        csrf = h.csrf()
        for method, path, body in (
            ("post", APPROVE, APPROVE_BODY),
            ("post", "/api/v1/automation/plans/plan-1/cancel", None),
            ("delete", "/api/v1/automation/plans/plan-1", None),
            ("post", "/api/v1/automation/plans/plan-1/delivery", {"pull_request_url": "https://example.test/pr/1"}),
            ("patch", "/api/v1/recommendations/1/ignore", {"reason": "x"}),
            ("delete", "/api/v1/recommendations/1/ignore", None),
            ("patch", "/api/v1/recommendations/1/apply", {}),
            ("post", "/api/v1/automation/repositories/allowlist", {"repositories": []}),
        ):
            response = h.client.request(method.upper(), path, json=body, headers=csrf)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


_EXEMPT_MUTATIONS = {
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/password",
    "/api/v1/auth/sessions/{session_id}",
    "/api/v1/webhooks/{provider_id}",
    "/api/v1/config/services",
}


def test_a_viewer_cannot_call_any_mutating_route(tmp_path, monkeypatch):
    """Every POST/PUT/PATCH/DELETE route of the app answers 403 to a viewer session (signals need their scope)."""
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login("viewer", VIEWER_PASSWORD)
        csrf = h.csrf()
        checked = 0
        for route_path, operations in h.client.app.openapi()["paths"].items():
            if route_path in _EXEMPT_MUTATIONS:
                continue
            for method in set(operations) - {"get", "head", "options"}:
                path = route_path.replace("{", "").replace("}", "")
                response = h.client.request(method.upper(), path, json={}, headers=csrf)
                assert response.status_code == 403, (method, route_path, response.status_code, response.text)
                checked += 1
        assert checked >= 20


# --------------------------------------------------------------------------- #
# Real deployment: uvicorn applies X-Forwarded-For before the app             #
# --------------------------------------------------------------------------- #


def _served_like_greenkube_api(app):
    """Wrap the app exactly as ``greenkube-api`` serves it (forwarded headers of trusted peers applied)."""
    from greenkube.api.app import server_app

    return server_app(app, PROXY_ENV["TRUSTED_PROXY_IPS"])


def test_proxied_requests_with_x_forwarded_for_are_accepted(tmp_path, monkeypatch):
    """oauth2-proxy and ingress-nginx always add X-Forwarded-For: the peer check must see the proxy, not the user."""
    with auth_harness(
        tmp_path, monkeypatch, client_address=PROXY, asgi_wrapper=_served_like_greenkube_api, **PROXY_ENV
    ) as h:
        headers = {**_headers("alice", "sre"), "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
        me = h.client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["name"] == "alice" and me.json()["role"] == "operator"


def test_a_client_inside_a_trusted_range_does_not_pass_for_the_proxy(tmp_path, monkeypatch):
    """An untrusted peer claiming a trusted X-Forwarded-For address is still an untrusted peer."""
    with auth_harness(
        tmp_path, monkeypatch, client_address=OUTSIDER, asgi_wrapper=_served_like_greenkube_api, **PROXY_ENV
    ) as h:
        headers = {**_headers("admin", "platform-admins"), "X-Forwarded-For": "10.0.0.5"}
        response = h.client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "UNTRUSTED_PROXY_HEADER"


def test_transport_peer_is_recorded_before_the_forwarded_rewrite():
    import asyncio

    from greenkube.auth.peer import TRANSPORT_PEER_SCOPE_KEY, TransportPeerMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    scope = {
        "type": "http",
        "client": ("10.0.0.5", 41000),
        "headers": [(b"x-forwarded-for", b"203.0.113.9")],
    }
    asyncio.run(TransportPeerMiddleware(app, "10.0.0.0/24")(scope, None, None))
    assert seen["client"][0] == "203.0.113.9"
    assert seen[TRANSPORT_PEER_SCOPE_KEY] == "10.0.0.5"


# --------------------------------------------------------------------------- #
# Header families are never mixed                                              #
# --------------------------------------------------------------------------- #

TWO_FAMILIES = {
    **PROXY_ENV,
    "AUTH_PROXY_USER_HEADERS": "X-Forwarded-User,X-Auth-Request-User",
    "AUTH_PROXY_GROUPS_HEADERS": "X-Forwarded-Groups,X-Auth-Request-Groups",
}


def test_default_headers_ignore_another_family(tmp_path, monkeypatch):
    """A client-supplied X-Auth-Request-* next to the proxy's X-Forwarded-* adds no identity or group."""
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        headers = {
            **_headers("bob", "devs"),
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": "platform-admins",
        }
        me = h.client.get("/api/v1/auth/me", headers=headers).json()
        assert me["name"] == "bob" and me["role"] == "viewer"


def test_mixed_header_families_are_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **TWO_FAMILIES) as h:
        spoofed = {
            **_headers("bob", "devs"),
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": "platform-admins",
        }
        response = h.client.get("/api/v1/auth/me", headers=spoofed)
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "INVALID_IDENTITY"
        # A groups header of the other family alone is refused too (no group union).
        groups_only = {**_headers("bob", "devs"), "X-Auth-Request-Groups": "platform-admins"}
        assert h.client.get("/api/v1/auth/me", headers=groups_only).status_code == 401
        # The same user in both families is still ambiguous.
        same_user = {**_headers("bob", "devs"), "X-Auth-Request-User": "bob"}
        assert h.client.get("/api/v1/auth/me", headers=same_user).status_code == 401
        # One family alone works, with its own groups only.
        alone = {"X-Auth-Request-User": "carol", "X-Auth-Request-Groups": "sre"}
        me = h.client.get("/api/v1/auth/me", headers=alone).json()
        assert me["name"] == "carol" and me["role"] == "operator"


def test_repeated_user_headers_are_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=PROXY, **PROXY_ENV) as h:
        response = h.client.get("/api/v1/auth/me", headers=[("X-Forwarded-User", "bob"), ("X-Forwarded-User", "alice")])
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "INVALID_IDENTITY"


def test_more_groups_headers_than_user_headers_is_rejected():
    from greenkube.core.config import Config

    with pytest.raises(ValueError, match="AUTH_PROXY_GROUPS_HEADERS"):
        Config(
            API_AUTH_MODE="proxy",
            TRUSTED_PROXY_IPS="10.0.0.0/24",
            AUTH_PROXY_USER_HEADERS="X-Forwarded-User",
            AUTH_PROXY_GROUPS_HEADERS="X-Forwarded-Groups,X-Auth-Request-Groups",
        )


def test_a_proxy_session_is_not_valid_around_the_proxy(tmp_path, monkeypatch):
    """The session cookie of a proxied user, replayed directly against the API, is refused."""
    peer = {"address": PROXY}

    def switchable_peer(app):
        async def wrapped(scope, receive, send):
            if scope["type"] == "http":
                scope["client"] = peer["address"]
            await app(scope, receive, send)

        return wrapped

    with auth_harness(tmp_path, monkeypatch, asgi_wrapper=switchable_peer, **PROXY_ENV) as h:
        assert h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).status_code == 200
        peer["address"] = OUTSIDER
        direct = h.client.get("/api/v1/auth/me")
        assert direct.status_code == 401
        assert direct.json()["detail"]["code"] == "UNTRUSTED_PROXY_HEADER"
        # Back through the proxy the session still works.
        peer["address"] = PROXY
        assert h.client.get("/api/v1/auth/me", headers=_headers("alice", "sre")).status_code == 200
