"""Native OpenID Connect sign-in (roadmap F16, ``API_AUTH_MODE=oidc``).

Authorization code flow with PKCE (S256), discovery, ``state`` bound to the
browser by a short-lived cookie and stored server-side with the ``nonce`` and
the code verifier (so any API replica can finish the sign-in), and ID tokens
verified against the provider's JWKS (RS*, PS*, ES* or EdDSA signatures; never
``none`` or shared-secret algorithms). The result is an ordinary server-side
session, exactly like a password sign-in.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from greenkube.auth.roles import clean_groups
from greenkube.auth.settings import AuthSettings
from greenkube.auth.tokens import constant_time_equals

logger = logging.getLogger(__name__)

LOGIN_TTL = timedelta(minutes=10)
METADATA_TTL_SECONDS = 3600.0
#: Minimum interval between two JWKS refreshes triggered by an unknown ``kid``.
JWKS_REFRESH_INTERVAL_SECONDS = 60.0
CLOCK_LEEWAY_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 512 * 1024
CALLBACK_PATH = "api/v1/auth/oidc/callback"
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


class OidcError(Exception):
    """A sign-in step failed. ``code`` is safe to show in the UI; details go to the log only."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class OidcIdentity:
    subject: str
    username: str
    groups: frozenset[str]
    claims: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# PKCE, state and redirect helpers                                            #
# --------------------------------------------------------------------------- #


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or any(ch in value for ch in "=+/"):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_code_verifier() -> str:
    """A 43 to 128 character PKCE verifier (RFC 7636): 64 random bytes give 86 characters."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def safe_return_to(value: str | None) -> str:
    """Keep only a local path, so the callback can never redirect to another site."""
    text = (value or "").strip()
    if (
        not text.startswith("/")
        or text.startswith("//")
        or "\\" in text
        or len(text) > 512
        or any(ord(ch) < 32 for ch in text)
        or text.startswith("/api/")
    ):
        return "/"
    parts = urlsplit(text)
    if parts.scheme or parts.netloc:
        return "/"
    return text


def _require_url(value: Any, name: str, *, allow_http: bool) -> str:
    if not isinstance(value, str) or not value:
        raise OidcError("provider_unavailable", f"discovery document lacks {name}")
    parts = urlsplit(value)
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise OidcError("provider_unavailable", f"{name} must use https")
    if not parts.netloc:
        raise OidcError("provider_unavailable", f"{name} is not an absolute URL")
    return value


# --------------------------------------------------------------------------- #
# JSON Web Signature verification                                             #
# --------------------------------------------------------------------------- #


def _hash_for(alg: str):
    from cryptography.hazmat.primitives import hashes

    return {"256": hashes.SHA256(), "384": hashes.SHA384(), "512": hashes.SHA512()}[alg[-3:]]


def _int(value: str) -> int:
    return int.from_bytes(b64url_decode(value), "big")


def public_key_from_jwk(jwk: dict[str, Any]):
    """Build a ``cryptography`` public key from an RSA, EC or OKP (Ed25519) JWK."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    kty = jwk.get("kty")
    if kty == "RSA":
        return rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
    if kty == "EC":
        curve = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1()}.get(jwk.get("crv", ""))
        if curve is None:
            raise ValueError("unsupported curve")
        return ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), curve).public_key()
    if kty == "OKP" and jwk.get("crv") == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))
    raise ValueError("unsupported key type")


def _key_matches(jwk: dict[str, Any], alg: str) -> bool:
    if jwk.get("use") not in (None, "sig"):
        return False
    if jwk.get("alg") not in (None, alg):
        return False
    kty = jwk.get("kty")
    if alg.startswith(("RS", "PS")):
        return kty == "RSA"
    if alg.startswith("ES"):
        return kty == "EC"
    return kty == "OKP"


def _verify_signature(alg: str, key, signature: bytes, signing_input: bytes) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if alg.startswith("RS") and isinstance(key, rsa.RSAPublicKey):
        key.verify(signature, signing_input, padding.PKCS1v15(), _hash_for(alg))
    elif alg.startswith("PS") and isinstance(key, rsa.RSAPublicKey):
        digest = _hash_for(alg)
        key.verify(
            signature, signing_input, padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size), digest
        )
    elif alg.startswith("ES") and isinstance(key, ec.EllipticCurvePublicKey):
        size = (key.curve.key_size + 7) // 8
        if len(signature) != 2 * size:
            raise ValueError("bad ECDSA signature length")
        r = int.from_bytes(signature[:size], "big")
        s = int.from_bytes(signature[size:], "big")
        key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(_hash_for(alg)))
    elif alg == "EdDSA" and isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, signing_input)
    else:
        raise ValueError("key does not match the algorithm")


def decode_jws(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Split a compact JWS into ``(header, claims, signature, signing_input)`` without verifying it."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
        signature = b64url_decode(signature_b64)
    except (ValueError, TypeError, AttributeError) as error:
        raise OidcError("invalid_token", "malformed ID token") from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("invalid_token", "malformed ID token")
    return header, claims, signature, f"{header_b64}.{payload_b64}".encode("ascii")


def verify_jws(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Verify the signature of a compact JWS against a JWK set and return its claims."""
    header, claims, signature, signing_input = decode_jws(token)
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise OidcError("invalid_token", f"algorithm {alg!r} is not accepted")
    if header.get("crit"):
        raise OidcError("invalid_token", "critical header parameters are not supported")
    kid = header.get("kid")
    keys = [key for key in jwks.get("keys", []) if isinstance(key, dict) and _key_matches(key, str(alg))]
    if kid is not None:
        keys = [key for key in keys if key.get("kid") == kid]
    elif len(keys) != 1:
        raise OidcError("invalid_token", "the token names no key and the key set is ambiguous")
    if not keys:
        raise OidcError("unknown_key", f"no signing key {kid!r}")
    for jwk in keys:
        try:
            _verify_signature(str(alg), public_key_from_jwk(jwk), signature, signing_input)
            return claims
        except Exception:  # InvalidSignature, malformed key: try the next candidate
            continue
    raise OidcError("invalid_token", "signature verification failed")


def validate_id_token_claims(claims: dict[str, Any], *, issuer: str, client_id: str, nonce: str, now: datetime) -> None:
    """OpenID Connect Core 3.1.3.7 checks: issuer, audience, authorized party, expiry and nonce."""
    if claims.get("iss") != issuer:
        raise OidcError("invalid_token", "issuer mismatch")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    if client_id not in audiences:
        raise OidcError("invalid_token", "audience mismatch")
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and azp != client_id:
        raise OidcError("invalid_token", "authorized party mismatch")
    timestamp = now.timestamp()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + CLOCK_LEEWAY_SECONDS < timestamp:
        raise OidcError("invalid_token", "the ID token has expired")
    iat = claims.get("iat")
    if iat is not None and (not isinstance(iat, (int, float)) or iat - CLOCK_LEEWAY_SECONDS > timestamp):
        raise OidcError("invalid_token", "the ID token was issued in the future")
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or nbf - CLOCK_LEEWAY_SECONDS > timestamp):
        raise OidcError("invalid_token", "the ID token is not valid yet")
    if not constant_time_equals(claims.get("nonce") if isinstance(claims.get("nonce"), str) else None, nonce):
        raise OidcError("invalid_token", "nonce mismatch")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise OidcError("invalid_token", "the ID token has no subject")


def identity_from_claims(claims: dict[str, Any], settings: AuthSettings) -> OidcIdentity:
    """The account behind verified claims.

    The username comes from ``OIDC_USERNAME_CLAIM``, then ``preferred_username``,
    ``email`` and ``sub``; the groups from ``OIDC_GROUPS_CLAIM``. The stable key
    of the account is the subject, never the username.
    """
    subject = str(claims["sub"])
    username = ""
    for claim in (settings.oidc_username_claim, "preferred_username", "email", "sub"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            username = value.strip().lower()
            break
    raw_groups = claims.get(settings.oidc_groups_claim)
    if isinstance(raw_groups, str):
        groups = clean_groups(raw_groups.split(","))
    elif isinstance(raw_groups, list):
        groups = clean_groups(raw_groups)
    else:
        groups = frozenset()
    return OidcIdentity(subject=subject, username=username[:128], groups=groups, claims=claims)


# --------------------------------------------------------------------------- #
# Provider client                                                             #
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OidcClient:
    """Discovery, JWKS and token endpoint calls for one configured provider."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._metadata: tuple[float, ProviderMetadata] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    @property
    def allow_http(self) -> bool:
        return self.settings.environment != "production" and self.settings.oidc_issuer.startswith("http://")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport)

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as error:
            raise OidcError("provider_unavailable", f"GET {url} failed ({type(error).__name__})") from error
        return self._json(response, f"GET {url}")

    @staticmethod
    def _json(response: httpx.Response, what: str, code: str = "provider_unavailable") -> dict[str, Any]:
        if response.status_code != 200:
            raise OidcError(code, f"{what} answered {response.status_code}")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise OidcError(code, f"{what} answered too much data")
        try:
            body = response.json()
        except ValueError as error:
            raise OidcError(code, f"{what} did not answer JSON") from error
        if not isinstance(body, dict):
            raise OidcError(code, f"{what} did not answer a JSON object")
        return body

    async def metadata(self) -> ProviderMetadata:
        cached = self._metadata
        if cached and self._monotonic() - cached[0] < METADATA_TTL_SECONDS:
            return cached[1]
        issuer = self.settings.oidc_issuer
        document = await self._get_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if document.get("issuer") != issuer:
            raise OidcError("provider_unavailable", "the discovery document names another issuer")
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise OidcError("provider_unavailable", "the provider does not support PKCE S256")
        auth_methods = document.get("token_endpoint_auth_methods_supported")
        metadata = ProviderMetadata(
            issuer=issuer,
            authorization_endpoint=_require_url(
                document.get("authorization_endpoint"), "authorization_endpoint", allow_http=self.allow_http
            ),
            token_endpoint=_require_url(document.get("token_endpoint"), "token_endpoint", allow_http=self.allow_http),
            jwks_uri=_require_url(document.get("jwks_uri"), "jwks_uri", allow_http=self.allow_http),
            token_endpoint_auth_methods=tuple(str(m) for m in auth_methods) if isinstance(auth_methods, list) else (),
        )
        self._metadata = (self._monotonic(), metadata)
        return metadata

    async def jwks(self, *, refresh: bool = False) -> dict[str, Any]:
        cached = self._jwks
        age = self._monotonic() - cached[0] if cached else None
        if cached and age is not None and (age < METADATA_TTL_SECONDS and not refresh):
            return cached[1]
        if cached and refresh and age is not None and age < JWKS_REFRESH_INTERVAL_SECONDS:
            return cached[1]
        async with self._lock:
            metadata = await self.metadata()
            document = await self._get_json(metadata.jwks_uri)
            if not isinstance(document.get("keys"), list):
                raise OidcError("provider_unavailable", "the JWKS document has no keys")
            self._jwks = (self._monotonic(), document)
            return document

    async def authorization_url(self, *, state: str, nonce: str, verifier: str, redirect_uri: str) -> str:
        metadata = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(dict.fromkeys(["openid", *self.settings.oidc_scopes.split()])),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        separator = "&" if urlsplit(metadata.authorization_endpoint).query else "?"
        return f"{metadata.authorization_endpoint}{separator}{urlencode(params)}"

    async def exchange_code(self, *, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
        metadata = await self.metadata()
        settings = self.settings
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": settings.oidc_client_id,
        }
        headers = {"Accept": "application/json"}
        if settings.oidc_client_secret:
            methods = metadata.token_endpoint_auth_methods
            if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
                form["client_secret"] = settings.oidc_client_secret
            else:
                # client_secret_basic: both parts are form-encoded first (RFC 6749 section 2.3.1).
                pair = f"{quote(settings.oidc_client_id, safe='')}:{quote(settings.oidc_client_secret, safe='')}"
                headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        try:
            async with self._client() as client:
                response = await client.post(metadata.token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as error:
            raise OidcError("provider_unavailable", f"token request failed ({type(error).__name__})") from error
        tokens = self._json(response, "the token endpoint", code="token_exchange_failed")
        if not isinstance(tokens.get("id_token"), str):
            raise OidcError("token_exchange_failed", "the token response has no id_token")
        token_type = tokens.get("token_type")
        if token_type is not None and str(token_type).lower() != "bearer":
            raise OidcError("token_exchange_failed", "unexpected token_type")
        return tokens

    async def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        jwks = await self.jwks()
        try:
            claims = verify_jws(id_token, jwks)
        except OidcError as error:
            if error.code != "unknown_key":
                raise
            # The provider rotated its keys: refresh the key set once.
            claims = verify_jws(id_token, await self.jwks(refresh=True))
        validate_id_token_claims(
            claims,
            issuer=self.settings.oidc_issuer,
            client_id=self.settings.oidc_client_id,
            nonce=nonce,
            now=self._clock(),
        )
        return claims


def get_oidc_client(app_state: Any, settings: AuthSettings) -> OidcClient:
    """One cached client per provider configuration (metadata and keys are cached inside)."""
    key = (settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret, settings.oidc_scopes)
    current = getattr(app_state, "oidc_client", None)
    if isinstance(current, OidcClient) and getattr(app_state, "oidc_client_key", None) == key:
        current.settings = settings
        return current
    transport = getattr(app_state, "oidc_transport", None)
    client = OidcClient(settings, transport=transport)
    app_state.oidc_client = client
    app_state.oidc_client_key = key
    return client
