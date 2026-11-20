"""Browser sign-in with an API key in ``API_AUTH_MODE=api_key`` (roadmap A.6, field report F-09).

The key is exchanged once for an HttpOnly, SameSite=Strict cookie: the UI
never keeps it in JavaScript or browser storage, and mutations still need the
CSRF token returned by the sign-in and ``/auth/me``.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

from tests.api.auth_support import ORIGIN, auth_harness  # pyrefly: ignore[missing-import]

COOKIE = "greenkube_session"
LEGACY_KEY = "legacy-bootstrap-key-0123456789"
API_KEY_MODE = {"API_AUTH_MODE": "api_key", "GREENKUBE_API_KEY": LEGACY_KEY}
LOGIN = "/api/v1/auth/api-key-login"


def _key_login(h, key: str, *, remember: bool = False, origin: str | None = ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return h.client.post(LOGIN, json={"key": key, "remember": remember}, headers=headers)


def test_providers_offer_the_api_key_field_in_api_key_mode(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        body = h.client.get("/api/v1/auth/providers").json()

    assert body["mode"] == "api_key"
    assert body["password"] is False
    assert body["api_key"] is True


def test_providers_do_not_offer_the_api_key_field_in_session_mode(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        body = h.client.get("/api/v1/auth/providers").json()

    assert body["api_key"] is False


def test_the_bootstrap_key_signs_the_browser_in_with_an_http_only_cookie(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        response = _key_login(h, LEGACY_KEY)
        assert response.status_code == 200, response.text
        me = h.client.get("/api/v1/auth/me")
        version = h.client.get("/api/v1/version")

    body = response.json()
    assert body["kind"] == "apikey" and body["authenticated"] is True
    assert body["auth_mode"] == "api_key"
    assert body["csrf_token"]
    assert LEGACY_KEY not in response.text
    assert response.headers["cache-control"] == "no-store"
    morsel = SimpleCookie(response.headers["set-cookie"])[COOKIE]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "strict"
    assert not morsel["max-age"]
    assert me.status_code == 200 and me.json()["kind"] == "apikey"
    assert me.json()["csrf_token"] == body["csrf_token"]
    assert version.status_code == 200


def test_a_scoped_key_signs_in_with_its_own_scopes(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        created = h.client.portal.call(lambda: h.service.create_api_key("ui-reader", ["read"], created_by="test"))
        response = _key_login(h, created.plaintext)
        assert response.status_code == 200, response.text
        keys = h.client.get("/api/v1/auth/api-keys")

    assert response.json()["name"] == "ui-reader"
    assert response.json()["scopes"] == ["read"]
    assert keys.status_code == 403


def test_a_wrong_key_is_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        response = _key_login(h, "not-the-key")
        me = h.client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert me.status_code == 401


def test_a_forged_cookie_is_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        h.client.cookies.set(COOKIE, "not-the-key")
        me = h.client.get("/api/v1/auth/me")

    assert me.status_code == 401


def test_a_cross_site_key_sign_in_is_refused(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        response = _key_login(h, LEGACY_KEY, origin="https://evil.example")

    assert response.status_code == 403


def test_key_sign_in_is_only_offered_in_api_key_mode(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY) as h:
        response = _key_login(h, LEGACY_KEY)

    assert response.status_code == 409


def test_key_sign_in_attempts_are_rate_limited(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, AUTH_LOGIN_RATE_LIMIT="2/minute", **API_KEY_MODE) as h:
        statuses = [_key_login(h, "wrong").status_code for _ in range(3)]

    assert statuses[-1] == 429


def test_cookie_authenticated_mutations_need_the_csrf_token(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        token = _key_login(h, LEGACY_KEY).json()["csrf_token"]
        without = h.client.post(
            "/api/v1/auth/api-keys", json={"name": "x", "scopes": ["read"]}, headers={"Origin": ORIGIN}
        )
        with_token = h.client.post(
            "/api/v1/auth/api-keys",
            json={"name": "x", "scopes": ["read"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": token},
        )

    assert without.status_code == 403
    assert without.json()["detail"]["code"] == "CSRF_REJECTED"
    assert with_token.status_code in (200, 201), with_token.text


def test_logout_clears_the_key_cookie(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        _key_login(h, LEGACY_KEY)
        out = h.client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        me = h.client.get("/api/v1/auth/me")

    assert out.status_code == 204
    assert me.status_code == 401


def test_a_revoked_scoped_key_stops_the_browser(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, **API_KEY_MODE) as h:
        created = h.client.portal.call(lambda: h.service.create_api_key("ui-reader", ["read"], created_by="test"))
        assert _key_login(h, created.plaintext).status_code == 200
        h.client.portal.call(h.service.revoke_api_key, created.key.id)
        me = h.client.get("/api/v1/auth/me")

    assert me.status_code == 401
