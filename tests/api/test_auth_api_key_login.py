"""Browser sign-in with an API key in ``API_AUTH_MODE=api_key`` (roadmap A.6, field report F-09).

The key is checked once and exchanged for a signed, expiring HttpOnly cookie
(``v1.<kid>.<exp>.<mac>``) that never contains the key. Every request checks
the signature and expiry and that the key is still active; mutations need the
CSRF token returned by the sign-in and ``/auth/me``.
"""

from __future__ import annotations

from contextlib import contextmanager
from http.cookies import SimpleCookie

from tests.api.auth_support import auth_harness  # pyrefly: ignore[missing-import]

COOKIE = "greenkube_session"
LEGACY_KEY = "legacy-bootstrap-key-0123456789"
API_KEY_MODE = {"API_AUTH_MODE": "api_key", "GREENKUBE_API_KEY": LEGACY_KEY}
LOGIN = "/api/v1/auth/api-key-login"
HTTPS_ORIGIN = "https://testserver"


@contextmanager
def _harness(tmp_path, monkeypatch, *, base_url: str = HTTPS_ORIGIN, **env: str):
    with auth_harness(tmp_path, monkeypatch, base_url=base_url, **{**API_KEY_MODE, **env}) as h:
        yield h


def _key_login(h, key: str, *, remember: bool = False, origin: str | None = HTTPS_ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return h.client.post(LOGIN, json={"key": key, "remember": remember}, headers=headers)


def _cookie(response):
    return SimpleCookie(response.headers["set-cookie"])[COOKIE]


def _scoped_key(h, scopes=("read",), name="ui-reader"):
    return h.client.portal.call(lambda: h.service.create_api_key(name, list(scopes), created_by="test"))


def _clears_cookie(response) -> bool:
    header = response.headers.get("set-cookie", "")
    return f'{COOKIE}=""' in header or f"{COOKIE}=;" in header


# --------------------------------------------------------------------------- #
# Sign-in options                                                             #
# --------------------------------------------------------------------------- #


def test_providers_offer_the_api_key_field_in_api_key_mode(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        body = h.client.get("/api/v1/auth/providers").json()

    assert body["mode"] == "api_key"
    assert body["password"] is False
    assert body["api_key"] is True


def test_providers_do_not_offer_the_api_key_field_in_session_mode(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        body = h.client.get("/api/v1/auth/providers").json()

    assert body["api_key"] is False


# --------------------------------------------------------------------------- #
# The signed cookie                                                           #
# --------------------------------------------------------------------------- #


def test_the_bootstrap_key_signs_in_with_a_signed_cookie_that_never_contains_the_key(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        response = _key_login(h, LEGACY_KEY)
        assert response.status_code == 200, response.text
        me = h.client.get("/api/v1/auth/me")
        version = h.client.get("/api/v1/version")

    body = response.json()
    assert body["kind"] == "apikey" and body["authenticated"] is True
    assert body["auth_mode"] == "api_key"
    assert body["csrf_token"]
    assert response.headers["cache-control"] == "no-store"
    morsel = _cookie(response)
    assert LEGACY_KEY not in morsel.value and LEGACY_KEY not in response.text
    version_tag, kid, exp, mac = morsel.value.split(".")
    assert (version_tag, kid) == ("v1", "legacy") and exp.isdigit() and len(mac) >= 43
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["secure"] is True
    assert not morsel["max-age"]
    assert me.status_code == 200 and me.json()["kind"] == "apikey"
    assert me.json()["csrf_token"] == body["csrf_token"]
    assert version.status_code == 200


def test_a_scoped_key_signs_in_with_its_own_scopes_and_its_id_only_in_the_cookie(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        created = _scoped_key(h)
        response = _key_login(h, created.plaintext)
        assert response.status_code == 200, response.text
        keys = h.client.get("/api/v1/auth/api-keys")

    secret = created.plaintext.split("_", 2)[2]
    assert response.json()["name"] == "ui-reader"
    assert response.json()["scopes"] == ["read"]
    assert _cookie(response).value.split(".")[1] == created.key.id
    assert secret not in _cookie(response).value
    assert keys.status_code == 403


def test_the_cookie_expires_after_twelve_hours(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        assert _key_login(h, LEGACY_KEY).status_code == 200
        h.clock.advance(hours=11)
        before = h.client.get("/api/v1/auth/me")
        h.clock.advance(hours=2)
        after = h.client.get("/api/v1/auth/me")

    assert before.status_code == 200
    assert after.status_code == 401
    assert _clears_cookie(after)


def test_keep_me_signed_in_lasts_thirty_days(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        response = _key_login(h, LEGACY_KEY, remember=True)
        h.clock.advance(days=29)
        before = h.client.get("/api/v1/auth/me")
        h.clock.advance(days=2)
        after = h.client.get("/api/v1/auth/me")

    assert int(_cookie(response)["max-age"]) == 30 * 24 * 3600
    assert before.status_code == 200
    assert after.status_code == 401


def test_a_tampered_cookie_is_refused_and_cleared(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        value = _cookie(_key_login(h, LEGACY_KEY)).value
        version_tag, kid, exp, mac = value.split(".")
        forged = {
            "later expiry": f"{version_tag}.{kid}.{int(exp) + 86400}.{mac}",
            "other kid": f"{version_tag}.abcdefgh.{exp}.{mac}",
            "raw key": LEGACY_KEY,
            "garbage": "v1.legacy.notanumber.xyz",
        }
        results = {}
        for name, cookie in forged.items():
            h.client.cookies.clear()
            h.client.cookies.set(COOKIE, cookie)
            results[name] = h.client.get("/api/v1/auth/me")

    for name, response in results.items():
        assert response.status_code == 401, name
        assert _clears_cookie(response), name


def test_a_revoked_scoped_key_stops_the_browser(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        created = _scoped_key(h)
        assert _key_login(h, created.plaintext).status_code == 200
        h.client.portal.call(h.service.revoke_api_key, created.key.id)
        me = h.client.get("/api/v1/auth/me")

    assert me.status_code == 401


def test_rotating_the_bootstrap_key_signs_every_browser_out(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        created = _scoped_key(h)
        assert _key_login(h, LEGACY_KEY).status_code == 200
        legacy_cookie = h.client.cookies.get(COOKIE)
        h.client.cookies.clear()
        assert _key_login(h, created.plaintext).status_code == 200
        scoped_cookie = h.client.cookies.get(COOKIE)
        monkeypatch.setenv("GREENKUBE_API_KEY", "rotated-bootstrap-key-9876543210")
        h.config.reload()
        results = []
        for cookie in (legacy_cookie, scoped_cookie):
            h.client.cookies.clear()
            h.client.cookies.set(COOKIE, cookie)
            results.append(h.client.get("/api/v1/auth/me").status_code)

    assert results == [401, 401]


def test_the_csrf_token_differs_per_cookie_and_is_not_derived_from_the_key_alone(tmp_path, monkeypatch):
    import hashlib
    import hmac

    with _harness(tmp_path, monkeypatch) as h:
        first = _key_login(h, LEGACY_KEY).json()["csrf_token"]
        h.clock.advance(seconds=5)
        h.client.cookies.clear()
        second = _key_login(h, LEGACY_KEY).json()["csrf_token"]

    assert first != second
    oracle = hmac.new(LEGACY_KEY.encode(), b"greenkube-browser-key-csrf", hashlib.sha256).hexdigest()
    assert oracle not in (first, second)


def test_cookie_authenticated_mutations_need_the_csrf_token_and_the_origin(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        token = _key_login(h, LEGACY_KEY).json()["csrf_token"]
        body = {"name": "x", "scopes": ["read"]}
        without = h.client.post("/api/v1/auth/api-keys", json=body, headers={"Origin": HTTPS_ORIGIN})
        foreign = h.client.post(
            "/api/v1/auth/api-keys", json=body, headers={"Origin": "https://evil.example", "X-CSRF-Token": token}
        )
        with_token = h.client.post(
            "/api/v1/auth/api-keys", json=body, headers={"Origin": HTTPS_ORIGIN, "X-CSRF-Token": token}
        )

    assert without.status_code == 403
    assert without.json()["detail"]["code"] == "CSRF_REJECTED"
    assert foreign.status_code == 403
    assert with_token.status_code in (200, 201), with_token.text


def test_logout_clears_the_key_cookie(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        _key_login(h, LEGACY_KEY)
        out = h.client.post("/api/v1/auth/logout", headers={"Origin": HTTPS_ORIGIN})
        me = h.client.get("/api/v1/auth/me")

    assert out.status_code == 204
    assert _clears_cookie(out)
    assert me.status_code == 401


def test_keys_with_cookie_special_or_non_latin1_characters_sign_in(tmp_path, monkeypatch):
    statuses = []
    for index, key in enumerate(['we;ird,key "with" spaces\\and=signs', "clé-€-0123456789abcdef"]):
        (tmp_path / str(index)).mkdir()
        with _harness(tmp_path / str(index), monkeypatch, GREENKUBE_API_KEY=key) as h:
            statuses.append((_key_login(h, key).status_code, h.client.get("/api/v1/auth/me").status_code))

    assert statuses == [(200, 200), (200, 200)]


# --------------------------------------------------------------------------- #
# Refusals                                                                    #
# --------------------------------------------------------------------------- #


def test_a_wrong_key_is_refused(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        response = _key_login(h, "not-the-key")
        me = h.client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert me.status_code == 401


def test_a_cross_site_key_sign_in_is_refused(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        response = _key_login(h, LEGACY_KEY, origin="https://evil.example")

    assert response.status_code == 403


def test_plain_http_to_a_remote_host_is_refused(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch, base_url="http://testserver") as h:
        response = _key_login(h, LEGACY_KEY, origin="http://testserver")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "HTTPS_REQUIRED"
    assert "set-cookie" not in response.headers


def test_plain_http_on_loopback_is_accepted(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch, base_url="http://localhost:8080") as h:
        response = _key_login(h, LEGACY_KEY, origin="http://localhost:8080")
        me = h.client.get("/api/v1/auth/me")

    assert response.status_code == 200, response.text
    assert me.status_code == 200


def test_key_sign_in_is_only_offered_in_api_key_mode(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, base_url=HTTPS_ORIGIN, GREENKUBE_API_KEY=LEGACY_KEY) as h:
        response = _key_login(h, LEGACY_KEY)

    assert response.status_code == 409


def test_key_sign_in_needs_a_bootstrap_key_to_sign_cookies(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch, GREENKUBE_API_KEY="") as h:
        created = _scoped_key(h)
        response = _key_login(h, created.plaintext)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "API_KEY_LOGIN_UNAVAILABLE"


def test_a_session_mode_cookie_holding_a_key_is_not_accepted(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch, GREENKUBE_API_KEY=LEGACY_KEY) as h:
        h.client.cookies.set(COOKIE, LEGACY_KEY)
        me = h.client.get("/api/v1/auth/me")

    assert me.status_code == 401


def test_credentials_without_the_read_scope_cannot_open_the_ui(tmp_path, monkeypatch):
    scrape = "scrape-token-abcdef0123456789"
    with _harness(tmp_path, monkeypatch, GREENKUBE_METRICS_TOKEN=scrape) as h:
        token = _key_login(h, scrape)
        metrics_key = _key_login(h, _scoped_key(h, scopes=("metrics:read",), name="scraper").plaintext)

    for response in (token, metrics_key):
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
        assert "set-cookie" not in response.headers


# --------------------------------------------------------------------------- #
# Rate limit                                                                  #
# --------------------------------------------------------------------------- #


def test_wrong_keys_from_one_client_do_not_lock_out_a_valid_key_on_the_same_ip(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch) as h:
        created = _scoped_key(h)
        wrong = [_key_login(h, guess).status_code for guess in ("gk_wrong", "typo", "typo2", "gk_wrong", "x")]
        valid = _key_login(h, created.plaintext)

    assert set(wrong) <= {401, 429}
    assert valid.status_code == 200, valid.text


def test_repeated_attempts_with_the_same_key_are_rate_limited(tmp_path, monkeypatch):
    with _harness(tmp_path, monkeypatch, AUTH_LOGIN_RATE_LIMIT="2/minute") as h:
        statuses = [_key_login(h, "gk_abcdefgh_wrongsecretwrongsecret00").status_code for _ in range(3)]

    assert statuses[-1] == 429
