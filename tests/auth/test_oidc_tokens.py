"""ID token signature and claim checks, and group to role mapping (roadmap F16)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from greenkube.auth.oidc import (
    OidcError,
    code_challenge,
    identity_from_claims,
    new_code_verifier,
    safe_return_to,
    validate_id_token_claims,
    verify_jws,
)
from greenkube.auth.roles import parse_role_groups, role_for_groups, split_groups_header
from greenkube.auth.settings import AuthSettings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int(value: int, size: int | None = None) -> str:
    return _b64(value.to_bytes(size or (value.bit_length() + 7) // 8, "big"))


def _signing_input(alg: str, claims: dict, kid: str = "k") -> str:
    header = {"alg": alg, "kid": kid}
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"


CLAIMS = {"sub": "123", "iss": "https://idp", "aud": "gk"}


def test_es256_tokens_verify_and_tampering_fails():
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwks = {"keys": [{"kty": "EC", "crv": "P-256", "kid": "k", "x": _int(numbers.x, 32), "y": _int(numbers.y, 32)}]}
    signing_input = _signing_input("ES256", CLAIMS)
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    assert verify_jws(token, jwks) == CLAIMS
    tampered = _signing_input("ES256", {**CLAIMS, "sub": "admin"}) + "." + token.rsplit(".", 1)[1]
    with pytest.raises(OidcError):
        verify_jws(tampered, jwks)


def test_ps256_and_eddsa_tokens_verify():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = rsa_key.public_key().public_numbers()
    signing_input = _signing_input("PS256", CLAIMS)
    signature = rsa_key.sign(
        signing_input.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
    )
    jwks = {"keys": [{"kty": "RSA", "kid": "k", "n": _int(n.n), "e": _int(n.e)}]}
    assert verify_jws(f"{signing_input}.{_b64(signature)}", jwks) == CLAIMS

    ed_key = ed25519.Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    raw = ed_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signing_input = _signing_input("EdDSA", CLAIMS)
    jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "k", "x": _b64(raw)}]}
    assert verify_jws(f"{signing_input}.{_b64(ed_key.sign(signing_input.encode()))}", jwks) == CLAIMS


def test_a_key_of_the_wrong_type_or_use_is_never_used():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = rsa_key.public_key().public_numbers()
    signing_input = _signing_input("RS256", CLAIMS)
    token = f"{signing_input}.{_b64(rsa_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))}"
    for jwk in (
        {"kty": "RSA", "kid": "k", "use": "enc", "n": _int(n.n), "e": _int(n.e)},
        {"kty": "RSA", "kid": "k", "alg": "RS512", "n": _int(n.n), "e": _int(n.e)},
    ):
        with pytest.raises(OidcError):
            verify_jws(token, {"keys": [jwk]})


def _valid_claims(now: datetime) -> dict:
    return {
        "iss": "https://idp",
        "aud": "gk",
        "sub": "123",
        "nonce": "n-1",
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iat": int(now.timestamp()),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://other"},
        {"aud": "other"},
        {"aud": ["gk", "other"]},
        {"nonce": "n-2"},
        {"nonce": None},
        {"sub": ""},
        {"exp": "soon"},
    ],
)
def test_claim_checks_fail_closed(change):
    now = datetime.now(timezone.utc)
    claims = {**_valid_claims(now), **change}
    with pytest.raises(OidcError):
        validate_id_token_claims(claims, issuer="https://idp", client_id="gk", nonce="n-1", now=now)


def test_claim_checks_accept_a_valid_token_and_clock_skew():
    now = datetime.now(timezone.utc)
    claims = _valid_claims(now)
    validate_id_token_claims(claims, issuer="https://idp", client_id="gk", nonce="n-1", now=now)
    skewed = {**claims, "iat": int((now + timedelta(seconds=30)).timestamp())}
    validate_id_token_claims(skewed, issuer="https://idp", client_id="gk", nonce="n-1", now=now)
    with pytest.raises(OidcError):
        validate_id_token_claims(
            claims, issuer="https://idp", client_id="gk", nonce="n-1", now=now + timedelta(minutes=10)
        )


def test_pkce_verifier_and_challenge():
    verifier = new_code_verifier()
    assert 43 <= len(verifier) <= 128
    # RFC 7636 appendix B
    assert (
        code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/recommendations?x=1", "/recommendations?x=1"),
        ("", "/"),
        ("https://evil.test/", "/"),
        ("//evil.test", "/"),
        ("/\\evil.test", "/"),
        ("/api/v1/auth/logout", "/"),
        ("/a\nb", "/"),
    ],
)
def test_return_to_stays_on_this_site(value, expected):
    assert safe_return_to(value) == expected


def test_identity_claims_fallbacks():
    settings = AuthSettings(oidc_username_claim="upn", oidc_groups_claim="roles")
    identity = identity_from_claims({"sub": "s", "email": "Bob@Acme.test", "roles": ["a", "", 3, "b"]}, settings)
    assert identity.username == "bob@acme.test" and identity.groups == frozenset({"a", "b"})
    assert identity_from_claims({"sub": "s", "roles": "x,y"}, settings).groups == frozenset({"x", "y"})
    assert identity_from_claims({"sub": "only-sub"}, settings).username == "only-sub"


def test_role_mapping():
    mapping = parse_role_groups('{"admin": ["root-team"], "operator": "sre", "viewer": []}')
    assert mapping == {"admin": frozenset({"root-team"}), "operator": frozenset({"sre"})}
    assert role_for_groups({"sre", "root-team"}, mapping, "viewer") == "admin"
    assert role_for_groups({"sre"}, mapping, "viewer") == "operator"
    assert role_for_groups(set(), mapping, "viewer") == "viewer"
    assert role_for_groups(set(), mapping, "none") is None
    assert split_groups_header(" a, b ,,c ") == frozenset({"a", "b", "c"})
    assert parse_role_groups("") == {}


def test_auth_settings_never_grant_from_a_malformed_mapping():
    class Cfg:
        API_AUTH_MODE = "proxy"
        AUTH_ROLE_GROUPS = "{not json"
        AUTH_DEFAULT_ROLE = "superuser"

    settings = AuthSettings.from_config(Cfg())
    assert settings.role_groups == {}
    assert settings.default_role == "none"
    assert settings.sessions_enabled and settings.proxy_enabled and not settings.oidc_enabled
