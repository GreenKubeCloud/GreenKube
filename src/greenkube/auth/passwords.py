"""Password hashing with the standard library's scrypt (roadmap F14).

Hashes are self-describing strings ``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (salt and
hash in unpadded URL-safe base64), so the work factor can be raised later and
existing hashes keep verifying. Verification compares digests in constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass

# OWASP minimum for scrypt is N=2^17, r=8, p=1 (128 MiB). GreenKube's API pod
# defaults to 128-512 MiB, so N=2^15 (32 MiB per hash) keeps a burst of logins
# from exhausting memory; logins are rate-limited per IP and per username.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_GENERATED_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class ScryptParameters:
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P


DEFAULT_PARAMETERS = ScryptParameters()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _derive(password: str, salt: bytes, params: ScryptParameters) -> bytes:
    # scrypt needs about 128 * r * (n + p) bytes; leave headroom for OpenSSL.
    maxmem = 128 * params.r * (params.n + params.p + 2) + 1024 * 1024
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        dklen=HASH_BYTES,
        maxmem=maxmem,
    )


def hash_password(password: str, params: ScryptParameters = DEFAULT_PARAMETERS) -> str:
    """Return a salted scrypt hash of ``password``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, params)
    return f"scrypt${params.n}${params.r}${params.p}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Return whether ``password`` matches ``encoded`` (constant-time comparison)."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        params = ScryptParameters(n=int(n), r=int(r), p=int(p))
        expected = _b64decode(digest)
        actual = _derive(password, _b64decode(salt), params)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(encoded: str, params: ScryptParameters = DEFAULT_PARAMETERS) -> bool:
    """Whether a stored hash uses weaker parameters than ``params``."""
    try:
        scheme, n, r, p, _salt, _digest = encoded.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or (int(n), int(r), int(p)) != (params.n, params.r, params.p)


# Hash of a random password, verified when a username is unknown so that the
# response time does not reveal which usernames exist.
_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    """Spend the same time as a real verification for an unknown user."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)


def validate_new_password(password: str, *, username: str = "") -> str | None:
    """Return a user-facing reason why ``password`` is not acceptable, or ``None``."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must have at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"The password must have at most {MAX_PASSWORD_LENGTH} characters."
    if username and password.strip().lower() == username.strip().lower():
        return "The password must not be the username."
    return None


def generate_password(length: int = 24) -> str:
    """Return a random alphanumeric password."""
    return "".join(secrets.choice(_GENERATED_ALPHABET) for _ in range(length))
