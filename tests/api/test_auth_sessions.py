"""Browser sign-in with server-side sessions (roadmap F14).

Covers the security review checklist: cookie flags, CSRF, rate limits,
session fixation, expiry, revocation and the audit actor.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.auth.metrics import AUTH_FAILURES
from greenkube.auth.tokens import sha256_hex
from tests.api.auth_support import (  # pyrefly: ignore[missing-import]
    ADMIN_PASSWORD,
    ORIGIN,
    VIEWER_PASSWORD,
    auth_harness,
)

COOKIE = "greenkube_session"
LEGACY_KEY = "legacy-bootstrap-key"
API_KEY_MODE = {"API_AUTH_MODE": "api_key", "GREENKUBE_API_KEY": LEGACY_KEY}


def _set_cookie(response) -> SimpleCookie:
    header = response.headers.get("set-cookie")
    assert header, "the response must set the session cookie"
    cookie = SimpleCookie()
    cookie.load(header)
    return cookie


def _failures(reason: str) -> float:
    return AUTH_FAILURES.labels(reason=reason)._value.get()


# --------------------------------------------------------------------------- #
# Sign-in and cookie flags                                                     #
# --------------------------------------------------------------------------- #


def test_login_issues_an_http_only_strict_cookie_and_returns_the_csrf_token(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        response = h.login()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "user" and body["name"] == "admin" and body["role"] == "admin"
        assert body["actor"] == "user:admin"
        assert body["csrf_token"]
        assert response.headers["cache-control"] == "no-store"
        morsel = _set_cookie(response)[COOKIE]
        assert len(morsel.value) >= 43  # 256-bit opaque token
        assert morsel["httponly"] is True
        assert morsel["samesite"] == "strict"
        assert morsel["path"] == "/"
        assert not morsel["secure"]  # plain HTTP with SESSION_COOKIE_SECURE=auto
        assert not morsel["max-age"]  # browser-session cookie without "keep me signed in"
        assert ADMIN_PASSWORD not in response.text


def test_session_cookie_is_stored_hashed(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        token = _set_cookie(h.login())[COOKIE].value

        stored = h.client.portal.call(h.service.repository.get_session, sha256_hex(token))
        missing = h.client.portal.call(h.service.repository.get_session, token)

    assert stored is not None
    assert missing is None


def test_remember_me_keeps_the_cookie_for_the_remember_lifetime(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        morsel = _set_cookie(h.login(remember=True))[COOKIE]

    assert int(morsel["max-age"]) == 30 * 24 * 3600


def test_secure_flag_is_always_set_when_configured(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, SESSION_COOKIE_SECURE="always") as h:
        response = h.login()

    assert _set_cookie(response)[COOKIE]["secure"] is True


def test_secure_flag_follows_x_forwarded_proto_from_a_trusted_proxy(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=("10.0.0.7", 40000), TRUSTED_PROXY_IPS="10.0.0.0/8") as h:
        proxied = h.login(headers={"X-Forwarded-Proto": "https"})

    assert _set_cookie(proxied)[COOKIE]["secure"] is True


def test_x_forwarded_proto_from_an_untrusted_client_is_ignored(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, client_address=("192.0.2.10", 40000), TRUSTED_PROXY_IPS="10.0.0.0/8") as h:
        spoofed = h.login(headers={"X-Forwarded-Proto": "https"})

    assert not _set_cookie(spoofed)[COOKIE]["secure"]


def test_https_requests_get_a_secure_cookie(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, base_url="https://testserver") as h:
        response = h.login(headers={"Origin": "https://testserver"})

    assert _set_cookie(response)[COOKIE]["secure"] is True


@pytest.mark.parametrize(
    ("username", "password"),
    [("admin", "wrong-password"), ("nobody", "whatever-password"), ("ADMIN ", "wrong-password")],
)
def test_bad_credentials_get_one_generic_error(tmp_path, monkeypatch, username, password):
    with auth_harness(tmp_path, monkeypatch) as h:
        before = _failures("bad_credentials")
        response = h.login(username, password)

    assert response.status_code == 401
    assert response.json()["detail"]["message"] == "Invalid username or password."
    assert "set-cookie" not in response.headers
    assert _failures("bad_credentials") == before + 1


def test_usernames_are_case_insensitive(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        assert h.login(" Admin ").status_code == 200


def test_cross_site_login_is_rejected(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        response = h.login(headers={"Origin": "https://attacker.example"})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


# --------------------------------------------------------------------------- #
# Brute force                                                                  #
# --------------------------------------------------------------------------- #


def test_failed_logins_are_rate_limited_per_username_with_backoff(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, AUTH_LOGIN_RATE_LIMIT="3/minute") as h:
        statuses = [h.login("admin", f"wrong-{attempt}").status_code for attempt in range(3)]
        before = _failures("rate_limited")
        blocked = h.login("admin", ADMIN_PASSWORD)
        other_user = h.login("viewer", VIEWER_PASSWORD)

    assert statuses == [401, 401, 401]
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert _failures("rate_limited") == before + 1
    assert other_user.status_code == 200


def test_failed_logins_are_rate_limited_per_ip(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, AUTH_LOGIN_RATE_LIMIT="1/minute") as h:
        # The per-IP budget is four times the per-username one.
        statuses = [h.login(f"user-{index}", "wrong-password").status_code for index in range(5)]

    assert statuses[:4] == [401, 401, 401, 401]
    assert statuses[4] == 429


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #


def test_session_authenticates_reads_and_the_audit_actor_is_the_user(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        assert h.client.get("/api/v1/version").status_code == 401
        h.login()
        version = h.client.get("/api/v1/version")
        actor = h.client.get("/api/v1/test-support/actor")

    assert version.status_code == 200
    assert actor.json() == {"actor": "user:admin"}


def test_login_never_reuses_a_presented_token(tmp_path, monkeypatch):
    """Session fixation: a token planted before sign-in is revoked, not upgraded."""
    with auth_harness(tmp_path, monkeypatch) as h:
        planted = _set_cookie(h.login("viewer", VIEWER_PASSWORD))[COOKIE].value
        fresh = _set_cookie(h.login())[COOKIE].value

        h.client.cookies.set(COOKIE, planted)
        planted_after = h.client.get("/api/v1/version")

    assert fresh != planted
    assert planted_after.status_code == 401


def test_logout_revokes_the_session_server_side(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        token = _set_cookie(h.login())[COOKIE].value
        logout = h.client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        h.client.cookies.set(COOKIE, token)
        replay = h.client.get("/api/v1/version")

    assert logout.status_code == 204
    assert _set_cookie(logout)[COOKIE]["max-age"] == "0"
    assert replay.status_code == 401


def test_sessions_expire_after_the_idle_timeout_and_slide_with_activity(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, AUTH_SESSION_IDLE_TIMEOUT="12h") as h:
        h.login()
        h.clock.advance(hours=11)
        assert h.client.get("/api/v1/version").status_code == 200
        h.clock.advance(hours=11)
        assert h.client.get("/api/v1/version").status_code == 200  # slid at the previous request
        h.clock.advance(hours=12, seconds=1)
        assert h.client.get("/api/v1/version").status_code == 401


def test_sessions_end_at_the_absolute_lifetime_despite_activity(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, AUTH_SESSION_ABSOLUTE_LIFETIME="1d") as h:
        h.login()
        for _ in range(3):
            h.clock.advance(hours=7)
            assert h.client.get("/api/v1/version").status_code == 200
        h.clock.advance(hours=4)
        assert h.client.get("/api/v1/version").status_code == 401


def test_sessions_list_and_revoke(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        first_token = _set_cookie(h.login())[COOKIE].value
        h.client.cookies.clear()  # a second browser
        h.login()
        sessions = h.client.get("/api/v1/auth/sessions").json()
        others = [item for item in sessions if not item["current"]]

        revoke = h.client.delete(f"/api/v1/auth/sessions/{others[0]['id']}", headers=h.csrf())
        missing = h.client.delete("/api/v1/auth/sessions/does-not-exist", headers=h.csrf())
        remaining = h.client.get("/api/v1/auth/sessions").json()
        h.client.cookies.set(COOKIE, first_token)
        revoked = h.client.get("/api/v1/version")

    assert len(sessions) == 2
    assert sum(item["current"] for item in sessions) == 1
    assert sessions[0]["user_agent"]
    assert revoke.status_code == 204
    assert missing.status_code == 404
    assert [item["current"] for item in remaining] == [True]
    assert revoked.status_code == 401


# --------------------------------------------------------------------------- #
# CSRF                                                                         #
# --------------------------------------------------------------------------- #


def test_unsafe_requests_need_the_csrf_header_and_a_same_origin(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        token = h.csrf()["X-CSRF-Token"]
        path = "/api/v1/auth/sessions/does-not-exist"
        before = _failures("csrf")

        no_token = h.client.delete(path, headers={"Origin": ORIGIN})
        wrong_token = h.client.delete(path, headers={"Origin": ORIGIN, "X-CSRF-Token": "forged"})
        cross_site = h.client.delete(path, headers={"Origin": "https://attacker.example", "X-CSRF-Token": token})
        no_origin = h.client.delete(path, headers={"X-CSRF-Token": token})
        referer_ok = h.client.delete(path, headers={"Referer": f"{ORIGIN}/settings", "X-CSRF-Token": token})
        same_site = h.client.delete(path, headers={"Origin": ORIGIN, "X-CSRF-Token": token})

    for rejected in (no_token, wrong_token, cross_site, no_origin):
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "CSRF_REJECTED"
    assert referer_ok.status_code == 404  # passed CSRF, unknown session
    assert same_site.status_code == 404
    assert _failures("csrf") == before + 4


def test_a_cross_origin_form_post_is_rejected(tmp_path, monkeypatch):
    """F14 acceptance: an HTML form on another site cannot act as the signed-in user."""
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        response = h.client.post(
            "/api/v1/optimization/runs",
            data={"namespace": "default"},
            headers={"Origin": "https://attacker.example", "Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 403


def test_bearer_requests_are_exempt_from_csrf(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, GREENKUBE_API_KEY="legacy-bootstrap-key") as h:
        response = h.client.delete(
            "/api/v1/auth/api-keys/abcdefgh", headers={"Authorization": "Bearer legacy-bootstrap-key"}
        )

    assert response.status_code == 404  # authorized, no CSRF needed, unknown key


@pytest.mark.parametrize("cors_origins", ["*", "https://attacker.example,*"])
def test_wildcard_cors_never_lets_another_site_use_the_session(tmp_path, monkeypatch, cors_origins):
    """N4 + F14: a wildcard origin is never credentialed and never passes the CSRF origin check."""
    with auth_harness(tmp_path, monkeypatch, CORS_ORIGINS=cors_origins) as h:
        h.login()
        token = h.csrf()["X-CSRF-Token"]
        attacker = {"Origin": "https://evil.example"}
        preflight = h.client.options(
            "/api/v1/auth/sessions/x",
            headers={**attacker, "Access-Control-Request-Method": "DELETE"},
        )
        read = h.client.get("/api/v1/auth/me", headers=attacker)
        write = h.client.delete("/api/v1/auth/sessions/x", headers={**attacker, "X-CSRF-Token": token})

    assert "access-control-allow-credentials" not in preflight.headers
    assert "access-control-allow-credentials" not in read.headers
    assert write.status_code == 403


# --------------------------------------------------------------------------- #
# Passwords                                                                    #
# --------------------------------------------------------------------------- #


def test_first_sign_in_must_change_the_initial_password(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, users=[("admin", ADMIN_PASSWORD, "admin", True)]) as h:
        login = h.login()
        assert login.json()["must_change_password"] is True
        blocked = h.client.get("/api/v1/config")
        me = h.client.get("/api/v1/auth/me")
        weak = h.client.post(
            "/api/v1/auth/password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
            headers=h.csrf(),
        )
        changed = h.client.post(
            "/api/v1/auth/password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password"},
            headers=h.csrf(),
        )
        after = h.client.get("/api/v1/config")
        old_password = h.login(password=ADMIN_PASSWORD)
        new_password = h.login(password="a-much-better-password")

    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert me.status_code == 200
    assert weak.status_code == 422
    assert changed.status_code == 200, changed.text
    assert changed.json()["must_change_password"] is False
    assert after.status_code == 200
    assert old_password.status_code == 401
    assert new_password.status_code == 200


def test_password_change_revokes_every_session_of_the_user(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        first = _set_cookie(h.login())[COOKIE].value
        h.client.cookies.clear()
        h.login()
        changed = h.client.post(
            "/api/v1/auth/password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "rotated-password-1"},
            headers=h.csrf(),
        )
        new_cookie = _set_cookie(changed)[COOKIE].value
        still_signed_in = h.client.get("/api/v1/version")
        h.client.cookies.set(COOKIE, first)
        revoked = h.client.get("/api/v1/version")

    assert changed.status_code == 200
    assert new_cookie != first
    assert still_signed_in.status_code == 200
    assert revoked.status_code == 401


def test_password_change_requires_the_current_password(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login()
        response = h.client.post(
            "/api/v1/auth/password",
            json={"current_password": "not-the-password", "new_password": "rotated-password-1"},
            headers=h.csrf(),
        )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Roles and open access                                                        #
# --------------------------------------------------------------------------- #


def test_viewer_role_cannot_mutate(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login("viewer", VIEWER_PASSWORD)
        response = h.client.post("/api/v1/optimization/runs", json={}, headers=h.csrf())

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"


def test_production_without_accounts_store_fails_closed(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_AUTH_MODE", "session")
    from greenkube.core.config import config

    config.reload()
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/version").status_code == 503


def test_development_without_accounts_stays_open(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, users=[]) as h:
        me = h.client.get("/api/v1/auth/me")
        version = h.client.get("/api/v1/version")

    assert version.status_code == 200
    assert me.json()["kind"] == "anonymous"
    assert me.json()["authenticated"] is False


def test_api_key_mode_disables_browser_sign_in(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        response = h.login()

    assert response.status_code == 409
