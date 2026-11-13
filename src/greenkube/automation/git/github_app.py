# src/greenkube/automation/git/github_app.py
"""GitHub App authentication (roadmap F3).

A GitHub App replaces a personal token: no human account, a bot identity on
commits and pull requests, per-installation rate limits, and tokens that expire
after an hour. GreenKube needs only the App id and its private key, in the
default credentials Secret (``appId`` + ``privateKey``) or in an explicit
``automation.providers[]`` entry whose ``secretKey`` is ``privateKey`` (the App
id is then read from the ``appId`` key of the same Secret).

For every repository GreenKube talks to:

1. it signs a short-lived JSON Web Token (RS256, 9 minutes) with the private key;
2. it discovers the installation that covers the repository
   (``GET /repos/{owner}/{repo}/installation``), so one App installed in several
   organizations works without configuring installation ids;
3. it requests an installation token scoped to that single repository
   (``POST /app/installations/{id}/access_tokens`` with ``repositories``), and
   caches it until five minutes before it expires;
4. commits are authored by the App's bot (``<slug>[bot]``), so GitHub shows them
   as the App's verified bot activity.

The private key, the JSON Web Token and installation tokens are never logged or
included in errors.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from greenkube.automation.git.base import GitProviderError, GitProviderTransientError

GITHUB_APP_ID_KEY = "appId"
GITHUB_APP_PRIVATE_KEY = "privateKey"
DEFAULT_GITHUB_API = "https://api.github.com"
#: An installation token is renewed this long before it expires.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
#: GitHub accepts App JSON Web Tokens valid for at most 10 minutes.
_JWT_LIFETIME = timedelta(minutes=9)
_JWT_CLOCK_SKEW = timedelta(seconds=60)
_APP_ID = re.compile(r"^(?:[0-9]{1,20}|Iv[0-9A-Za-z._-]{1,64})$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TIMEOUT_SECONDS = 15.0


class GitHubAppError(GitProviderError):
    """A GitHub App credential or installation problem; never carries secret material."""

    code = "github-app-unavailable"


@dataclass(frozen=True)
class GitHubAppCredentials:
    """The App id and PEM private key; the key is hidden from ``repr``."""

    app_id: str
    private_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not _APP_ID.fullmatch(self.app_id.strip()):
            raise GitHubAppError("GitHub App id must be the numeric App id (or its client id).")
        if not isinstance(self.private_key, str) or "PRIVATE KEY" not in self.private_key:
            raise GitHubAppError("GitHub App private key must be the PEM file GitHub generated.")


@dataclass(frozen=True)
class InstallationToken:
    """An installation token scoped to one repository, and the App's bot identity."""

    token: str = field(repr=False)
    expires_at: datetime
    installation_id: int
    repository: str
    bot_name: str
    bot_email: str

    def fresh(self, now: datetime) -> bool:
        return now + TOKEN_REFRESH_MARGIN < self.expires_at


def is_github_app_reference(provider_type: str | None, secret_key: str | None) -> bool:
    """A GitHub provider whose Secret reference points at a GitHub App private key."""
    return (provider_type or "").strip().lower() == "github" and secret_key == GITHUB_APP_PRIVATE_KEY


def app_jwt(credentials: GitHubAppCredentials, *, now: datetime) -> str:
    """RS256 JSON Web Token that authenticates as the App (not as an installation)."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        key = serialization.load_pem_private_key(credentials.private_key.strip().encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise GitHubAppError(
            "GitHub App private key could not be loaded (expected an unencrypted PEM RSA key)."
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError("GitHub App private key must be an RSA key.")
    header = {"alg": "RS256", "typ": "JWT"}
    issued = now - _JWT_CLOCK_SKEW
    payload = {
        "iat": int(issued.timestamp()),
        "exp": int((now + _JWT_LIFETIME).timestamp()),
        "iss": credentials.app_id.strip(),
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')))}.{_b64url(json.dumps(payload, separators=(',', ':')))}"
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


class GitHubAppTokenIssuer:
    """Discovers installations and caches repository-scoped installation tokens per process."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._transport = transport
        self._tokens: dict[tuple[str, str, str], InstallationToken] = {}
        self._bots: dict[tuple[str, str], tuple[str, str]] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def clear(self) -> None:
        self._tokens.clear()
        self._bots.clear()

    async def token_for(
        self,
        credentials: GitHubAppCredentials,
        *,
        api_base_url: str | None,
        owner: str,
        name: str,
    ) -> InstallationToken:
        """A cached or new installation token that can access only ``owner/name``."""
        if not _REPOSITORY_PART.fullmatch(owner or "") or not _REPOSITORY_PART.fullmatch(name or ""):
            raise GitHubAppError("GitHub App tokens are issued for one owner/name repository.")
        base = (api_base_url or DEFAULT_GITHUB_API).rstrip("/")
        key = (credentials.app_id.strip(), base, f"{owner}/{name}".lower())
        cached = self._tokens.get(key)
        if cached is not None and cached.fresh(self._clock()):
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            now = self._clock()
            if cached is not None and cached.fresh(now):
                return cached
            token = await self._issue(credentials, base, owner, name, now)
            self._tokens[key] = token
            return token

    async def _issue(
        self, credentials: GitHubAppCredentials, base: str, owner: str, name: str, now: datetime
    ) -> InstallationToken:
        jwt = app_jwt(credentials, now=now)
        app_headers = _headers(jwt)
        async with httpx.AsyncClient(
            base_url=base, timeout=_TIMEOUT_SECONDS, transport=self._transport, follow_redirects=False
        ) as client:
            installation = await _json(
                client, "GET", f"/repos/{owner}/{name}/installation", app_headers, missing="the App is not installed"
            )
            installation_id = installation.get("id")
            if isinstance(installation_id, bool) or not isinstance(installation_id, int) or installation_id <= 0:
                raise GitHubAppError("GitHub returned an invalid App installation.")
            issued = await _json(
                client,
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                app_headers,
                body={"repositories": [name]},
                missing="the App installation was not found",
            )
            token = issued.get("token")
            expires_at = _parse_time(issued.get("expires_at"))
            if not isinstance(token, str) or not token or expires_at is None:
                raise GitHubAppError("GitHub returned an invalid installation token.")
            bot_name, bot_email = await self._bot_identity(client, credentials, base, app_headers, token)
        return InstallationToken(
            token=token,
            expires_at=expires_at,
            installation_id=installation_id,
            repository=f"{owner}/{name}",
            bot_name=bot_name,
            bot_email=bot_email,
        )

    async def _bot_identity(
        self,
        client: httpx.AsyncClient,
        credentials: GitHubAppCredentials,
        base: str,
        app_headers: dict[str, str],
        token: str,
    ) -> tuple[str, str]:
        key = (credentials.app_id.strip(), base)
        cached = self._bots.get(key)
        if cached is not None:
            return cached
        app = await _json(client, "GET", "/app", app_headers, missing="the App was not found")
        slug = app.get("slug")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise GitHubAppError("GitHub returned an invalid App slug.")
        login = f"{slug}[bot]"
        user = await _json(client, "GET", f"/users/{login}", _headers(token), missing="the App bot user was not found")
        user_id = user.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise GitHubAppError("GitHub returned an invalid App bot user.")
        host = "users.noreply.github.com"
        identity = (login, f"{user_id}+{login}@{host}")
        self._bots[key] = identity
        return identity


#: One issuer per process: tokens are cached across plan operations until they expire.
DEFAULT_TOKEN_ISSUER = GitHubAppTokenIssuer()

#: Bounds of the installation repository listing (100 per page).
_MAX_INSTALLATIONS = 100
_MAX_REPOSITORY_PAGES = 50


async def installation_repositories(
    credentials: GitHubAppCredentials,
    *,
    api_base_url: str | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """``owner/name`` of every repository the App's installations were granted.

    The repository selection made when installing the App ("All repositories" or
    "Only select repositories") is the administrator's allowlist; GreenKube uses
    it for the inferred github.com provider. For each installation
    (``GET /app/installations``) an installation token is requested without a
    repository restriction and ``GET /installation/repositories`` is paged. The
    tokens are used for this listing only and dropped. Raises
    :class:`GitHubAppError` or :class:`GitProviderTransientError`; never returns
    a partial selection.
    """
    base = (api_base_url or DEFAULT_GITHUB_API).rstrip("/")
    jwt = app_jwt(credentials, now=now or datetime.now(timezone.utc))
    app_headers = _headers(jwt)
    names: set[str] = set()
    async with httpx.AsyncClient(
        base_url=base, timeout=_TIMEOUT_SECONDS, transport=transport, follow_redirects=False
    ) as client:
        installations = await _json_list(
            client, "/app/installations", app_headers, params={"per_page": str(_MAX_INSTALLATIONS)}
        )
        for installation in installations[:_MAX_INSTALLATIONS]:
            installation_id = installation.get("id") if isinstance(installation, dict) else None
            if isinstance(installation_id, bool) or not isinstance(installation_id, int) or installation_id <= 0:
                raise GitHubAppError("GitHub returned an invalid App installation.")
            issued = await _json(
                client,
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                app_headers,
                body={},
                missing="the App installation was not found",
            )
            token = issued.get("token")
            if not isinstance(token, str) or not token:
                raise GitHubAppError("GitHub returned an invalid installation token.")
            for page in range(1, _MAX_REPOSITORY_PAGES + 1):
                listing = await _json(
                    client,
                    "GET",
                    f"/installation/repositories?per_page=100&page={page}",
                    _headers(token),
                    missing="the installation repositories were not found",
                )
                repositories = listing.get("repositories")
                if not isinstance(repositories, list):
                    raise GitHubAppError("GitHub returned an invalid installation repository list.")
                for repository in repositories:
                    full_name = repository.get("full_name") if isinstance(repository, dict) else None
                    if isinstance(full_name, str) and full_name.count("/") == 1:
                        names.add(full_name)
                total = listing.get("total_count")
                if len(repositories) < 100 or (isinstance(total, int) and page * 100 >= total):
                    break
            else:
                raise GitHubAppError("The App installation lists more repositories than GreenKube reads.")
    return names


async def _json_list(
    client: httpx.AsyncClient, path: str, headers: dict[str, str], *, params: dict[str, str]
) -> list[Any]:
    try:
        response = await client.request("GET", path, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise GitProviderTransientError(f"GitHub App request failed ({type(exc).__name__}).") from None
    if response.status_code in (429, 502, 503, 504):
        raise GitProviderTransientError(
            f"GitHub App request answered {response.status_code}; retry later.", status_code=response.status_code
        )
    if response.status_code == 401:
        raise GitHubAppError("GitHub rejected the App credentials (check appId and privateKey).", status_code=401)
    if response.status_code >= 400:
        raise GitHubAppError(f"GitHub App request answered {response.status_code}.", status_code=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise GitHubAppError("GitHub returned an invalid App response.") from None
    if not isinstance(payload, list):
        raise GitHubAppError("GitHub returned an invalid App response.")
    return payload


def _headers(bearer: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {bearer}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "greenkube",
    }


async def _json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    body: dict[str, Any] | None = None,
    missing: str,
) -> dict[str, Any]:
    try:
        response = await client.request(method, path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise GitProviderTransientError(f"GitHub App request failed ({type(exc).__name__}).") from None
    if response.status_code in (502, 503, 504) or response.status_code == 429:
        raise GitProviderTransientError(
            f"GitHub App request answered {response.status_code}; retry later.", status_code=response.status_code
        )
    if response.status_code == 404:
        raise GitHubAppError(f"GitHub App authentication failed: {missing}.", status_code=404)
    if response.status_code == 401:
        raise GitHubAppError("GitHub rejected the App credentials (check appId and privateKey).", status_code=401)
    if response.status_code >= 400:
        raise GitHubAppError(
            f"GitHub App request answered {response.status_code}; check the App permissions.",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError:
        raise GitHubAppError("GitHub returned an invalid App response.") from None
    if not isinstance(payload, dict):
        raise GitHubAppError("GitHub returned an invalid App response.")
    return payload


def _b64url(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
