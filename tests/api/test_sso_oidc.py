"""Native OpenID Connect sign-in against a mock identity provider (roadmap F16).

The provider is an ``httpx.MockTransport`` installed on ``app.state``: no
network. It checks the PKCE verifier against the challenge of the
authorization request and signs ID tokens with a test RSA key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.api.auth_support import ORIGIN, auth_harness  # pyrefly: ignore[missing-import]

ISSUER = "https://idp.example.test/realms/acme"
CLIENT_ID = "greenkube"
CLIENT_SECRET = "s3cret/with:chars"
OIDC_ENV = {
    "API_AUTH_MODE": "oidc",
    "OIDC_ISSUER": ISSUER,
    "OIDC_AUDIENCE": CLIENT_ID,
    "OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "OIDC_PROVIDER_NAME": "Acme SSO",
    "AUTH_ROLE_GROUPS": json.dumps({"admin": ["gk-admins"], "operator": ["gk-operators"]}),
}
CALLBACK = "/api/v1/auth/oidc/callback"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


@dataclass
class MockIdP:
    """Discovery, JWKS and token endpoints of a fake provider, with knobs to misbehave."""

    key: Any = field(default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048))
    kid: str = "key-1"
    subject: str = "f2b6c1e0-1111"
    claims: dict[str, Any] = field(
        default_factory=lambda: {"preferred_username": "Alice", "email": "alice@acme.test", "groups": ["gk-operators"]}
    )
    overrides: dict[str, Any] = field(default_factory=dict)
    header_overrides: dict[str, Any] = field(default_factory=dict)
    discovery_issuer: str = ISSUER
    requests: list[httpx.Request] = field(default_factory=list)
    authorizations: dict[str, dict[str, str]] = field(default_factory=dict)
    token_calls: int = 0

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _int_b64(numbers.n),
                    "e": _int_b64(numbers.e),
                }
            ]
        }

    def sign(self, claims: dict[str, Any], header: dict[str, Any] | None = None) -> str:
        head = {"alg": "RS256", "typ": "JWT", "kid": self.kid, **(header or {})}
        signing_input = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(claims).encode())}"
        signature = self.key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64(signature)}"

    def authorize(self, location: str) -> str:
        """Play the browser at the provider: record the request, return the code."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        code = f"code-{len(self.authorizations)}"
        self.authorizations[code] = params
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    "code_challenge_methods_supported": ["plain", "S256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                },
            )
        if url == f"{ISSUER}/protocol/openid-connect/certs":
            return httpx.Response(200, json=self.jwks())
        if url == f"{ISSUER}/protocol/openid-connect/token":
            self.token_calls += 1
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            expected = "Basic " + base64.b64encode(b"greenkube:s3cret%2Fwith%3Achars").decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            auth = self.authorizations.pop(form.get("code", ""), None)
            if auth is None or form.get("grant_type") != "authorization_code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = _b64(hashlib.sha256(form["code_verifier"].encode()).digest())
            if challenge != auth["code_challenge"] or form["redirect_uri"] != auth["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": self.subject,
                "iat": now,
                "exp": now + 300,
                "nonce": auth["nonce"],
                **self.claims,
                **self.overrides,
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "id_token": self.sign(claims, self.header_overrides),
                },
            )
        return httpx.Response(404)


def _start(h, idp: MockIdP, return_to: str = "/recommendations?tab=open"):
    response = h.client.get("/api/v1/auth/oidc/login", params={"return_to": return_to}, follow_redirects=False)
    assert response.status_code == 302, response.text
    return response, idp.authorize(response.headers["location"])


def _finish(h, response, code: str):
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    return h.client.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False), state


@pytest.fixture
def oidc(tmp_path, monkeypatch):
    idp = MockIdP()
    with auth_harness(tmp_path, monkeypatch, **OIDC_ENV) as h:
        h.client.app.state.oidc_transport = httpx.MockTransport(idp.handler)
        yield h, idp


def test_authorization_request_uses_pkce_state_and_nonce(oidc):
    h, idp = oidc
    response, _code = _start(h, idp)
    location = urlsplit(response.headers["location"])
    params = {k: v[0] for k, v in parse_qs(location.query).items()}

    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/protocol/openid-connect/auth"
    assert params["response_type"] == "code" and params["client_id"] == CLIENT_ID
    assert params["code_challenge_method"] == "S256" and len(params["code_challenge"]) == 43
    assert params["scope"].split() == ["openid", "profile", "email"]
    assert len(params["state"]) >= 43 and len(params["nonce"]) >= 43
    assert params["redirect_uri"] == f"{ORIGIN}{CALLBACK}"
    cookie = response.headers["set-cookie"]
    assert "greenkube_session_oidc=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/v1/auth/oidc/" in cookie
    # The verifier never leaves the server.
    assert "code_verifier" not in response.headers["location"]


def test_oidc_sign_in_creates_a_session_with_the_mapped_role(oidc):
    h, idp = oidc
    response, code = _start(h, idp)
    callback, _state = _finish(h, response, code)

    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/recommendations?tab=open"
    assert "greenkube_session=" in callback.headers.get("set-cookie", "")
    me = h.client.get("/api/v1/auth/me").json()
    assert me["name"] == "alice" and me["role"] == "operator" and me["auth_source"] == "oidc"
    assert me["actor"] == "user:alice" and me["csrf_token"]
    user = h.client.portal.call(h.service.repository.get_user_by_external_id, f"oidc:{idp.subject}")
    assert user is not None and user.username == "alice"


def test_group_changes_apply_at_the_next_sign_in(oidc):
    h, idp = oidc
    response, code = _start(h, idp)
    _finish(h, response, code)
    assert h.client.get("/api/v1/auth/me").json()["role"] == "operator"

    idp.claims["groups"] = ["gk-admins"]
    response, code = _start(h, idp)
    _finish(h, response, code)
    assert h.client.get("/api/v1/auth/me").json()["role"] == "admin"


def test_state_must_come_back_to_the_browser_that_started_the_sign_in(oidc):
    h, idp = oidc
    response, code = _start(h, idp)
    h.client.cookies.delete("greenkube_session_oidc", path="/api/v1/auth/oidc/")
    h.client.cookies.clear()
    callback, _state = _finish(h, response, code)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?sso_error=state_mismatch"
    assert idp.token_calls == 0


def test_a_state_is_single_use(oidc):
    h, idp = oidc
    response, code = _start(h, idp)
    first, state = _finish(h, response, code)
    assert first.headers["location"].startswith("/recommendations")
    h.client.cookies.set("greenkube_session_oidc", state, path="/api/v1/auth/oidc/")
    replay = h.client.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)
    assert replay.headers["location"] == "/?sso_error=state_expired"


def test_an_expired_state_is_refused(oidc):
    h, idp = oidc
    response, code = _start(h, idp)
    h.clock.advance(minutes=11)
    callback, _state = _finish(h, response, code)
    assert callback.headers["location"] == "/?sso_error=state_expired"
    assert idp.token_calls == 0


@pytest.mark.parametrize(
    ("overrides", "header"),
    [
        ({"nonce": "attacker-nonce"}, {}),
        ({"aud": "another-client"}, {}),
        ({"aud": [CLIENT_ID, "other"], "azp": "other"}, {}),
        ({"iss": "https://evil.example.test"}, {}),
        ({"exp": int(time.time()) - 3600}, {}),
        ({}, {"kid": "unknown-key"}),
    ],
    ids=["nonce", "audience", "azp", "issuer", "expired", "unknown-key"],
)
def test_invalid_id_tokens_are_refused(oidc, overrides, header):
    h, idp = oidc
    idp.overrides = overrides
    idp.header_overrides = header
    response, code = _start(h, idp)
    callback, _state = _finish(h, response, code)
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/?sso_error=")
    assert "greenkube_session=" not in callback.headers.get("set-cookie", "").replace("greenkube_session_oidc", "")
    assert h.client.get("/api/v1/auth/me").status_code == 401


def test_unsigned_and_shared_secret_tokens_are_refused(oidc, monkeypatch):
    h, idp = oidc

    def unsigned(claims, header=None):
        head = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        return f"{head}.{_b64(json.dumps(claims).encode())}."

    monkeypatch.setattr(idp, "sign", unsigned)
    response, code = _start(h, idp)
    callback, _state = _finish(h, response, code)
    assert callback.headers["location"] == "/?sso_error=invalid_token"

    def hs256(claims, header=None):
        import hmac

        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": idp.kid}).encode())
        body = _b64(json.dumps(claims).encode())
        mac = hmac.new(CLIENT_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        return f"{head}.{body}.{_b64(mac)}"

    monkeypatch.setattr(idp, "sign", hs256)
    response, code = _start(h, idp)
    callback, _state = _finish(h, response, code)
    assert callback.headers["location"] == "/?sso_error=invalid_token"


def test_a_token_signed_by_another_key_is_refused(oidc):
    h, idp = oidc
    real_jwks = idp.jwks()
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.jwks = lambda: real_jwks  # the provider still publishes the old key
    response, code = _start(h, idp)
    callback, _state = _finish(h, response, code)
    assert callback.headers["location"] == "/?sso_error=invalid_token"


def test_discovery_must_name_the_configured_issuer(oidc):
    h, idp = oidc
    idp.discovery_issuer = "https://evil.example.test"
    response = h.client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?sso_error=provider_unavailable"


def test_provider_errors_and_open_redirects(oidc):
    h, idp = oidc
    denied = h.client.get(CALLBACK, params={"error": "access_denied", "state": "x"}, follow_redirects=False)
    assert denied.headers["location"] == "/?sso_error=denied"

    for target in ("https://evil.example.test/", "//evil.example.test", "/\\evil", "/api/v1/auth/logout"):
        response, code = _start(h, idp, return_to=target)
        callback, _state = _finish(h, response, code)
        assert callback.headers["location"] == "/", target


def test_users_without_a_mapped_group_are_refused_when_the_default_is_none(tmp_path, monkeypatch):
    idp = MockIdP()
    idp.claims["groups"] = ["marketing"]
    with auth_harness(tmp_path, monkeypatch, AUTH_DEFAULT_ROLE="none", **OIDC_ENV) as h:
        h.client.app.state.oidc_transport = httpx.MockTransport(idp.handler)
        response, code = _start(h, idp)
        callback, _state = _finish(h, response, code)
        assert callback.headers["location"] == "/?sso_error=no_role"


def test_providers_offer_the_sso_button_and_local_accounts_still_sign_in(oidc):
    h, _idp = oidc
    assert h.client.get("/api/v1/auth/providers").json() == {
        "mode": "oidc",
        "password": True,
        "sso": {"kind": "oidc", "name": "Acme SSO", "login_path": "/auth/oidc/login"},
    }
    assert h.login().status_code == 200


def test_oidc_routes_are_absent_without_oidc(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        assert h.client.get("/api/v1/auth/oidc/login", follow_redirects=False).status_code == 404
        assert h.client.get("/api/v1/auth/providers").json()["sso"] is None


def test_oidc_mode_requires_issuer_and_client():
    from greenkube.core.config import Config

    with pytest.raises(ValueError, match="OIDC_ISSUER"):
        Config(API_AUTH_MODE="oidc", OIDC_ISSUER="", OIDC_AUDIENCE="x")
    with pytest.raises(ValueError, match="https"):
        Config(API_AUTH_MODE="oidc", OIDC_ISSUER="http://idp", OIDC_AUDIENCE="x", ENVIRONMENT="production")
