"""GitHub App authentication (roadmap F3): JWT, installation discovery, scoped cached tokens, bot identity."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from greenkube.automation.git.base import GitProviderTransientError, GitRepository
from greenkube.automation.git.credentials import GitProviderCredentialError, GitProviderCredentialResolver
from greenkube.automation.git.github_app import (
    GitHubAppCredentials,
    GitHubAppError,
    GitHubAppTokenIssuer,
    app_jwt,
    is_github_app_reference,
)
from greenkube.automation.git.registry import GitProviderRegistry
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsProviderRepository

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
).decode()
APP_ID = "123456"
INSTALLATION_TOKEN = "ghs_installationtoken0123456789"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _decode(segment: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class FakeGitHub:
    """The four GitHub App endpoints GreenKube calls, recording every request."""

    def __init__(self, *, clock, installed: bool = True) -> None:
        self.clock = clock
        self.installed = installed
        self.requests: list[httpx.Request] = []
        self.issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/repos/acme/gitops/installation":
            return httpx.Response(200, json={"id": 77}) if self.installed else httpx.Response(404, json={})
        if path == "/app/installations/77/access_tokens" and request.method == "POST":
            self.issued += 1
            expires = (self.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return httpx.Response(
                201,
                json={
                    "token": f"{INSTALLATION_TOKEN}{self.issued}",
                    "expires_at": expires,
                    "permissions": {"contents": "write", "pull_requests": "write"},
                },
            )
        if path == "/app":
            return httpx.Response(200, json={"id": 1, "slug": "greenkube-acme"})
        if path == "/users/greenkube-acme[bot]":
            return httpx.Response(200, json={"id": 4242, "login": "greenkube-acme[bot]"})
        return httpx.Response(404, json={})


def _issuer(fake: FakeGitHub, clock) -> GitHubAppTokenIssuer:
    return GitHubAppTokenIssuer(clock=clock, transport=httpx.MockTransport(fake.handler))


def test_app_jwt_is_rs256_signed_by_the_app_key_and_short_lived():
    now = _now()
    token = app_jwt(GitHubAppCredentials(APP_ID, PRIVATE_KEY), now=now)

    header, payload, signature = token.split(".")
    assert _decode(header) == {"alg": "RS256", "typ": "JWT"}
    claims = _decode(payload)
    assert claims["iss"] == APP_ID
    assert claims["iat"] < now.timestamp() < claims["exp"] <= (now + timedelta(minutes=10)).timestamp()
    _KEY.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_credentials_hide_the_private_key_and_reject_malformed_values():
    credentials = GitHubAppCredentials(APP_ID, PRIVATE_KEY)
    assert "PRIVATE" not in repr(credentials)
    with pytest.raises(GitHubAppError):
        GitHubAppCredentials("not an id", PRIVATE_KEY)
    with pytest.raises(GitHubAppError):
        GitHubAppCredentials(APP_ID, "ghp_token")
    broken = GitHubAppCredentials(APP_ID, "-----BEGIN RSA PRIVATE KEY-----\nnot a key\n-----END RSA PRIVATE KEY-----")
    with pytest.raises(GitHubAppError) as error:
        app_jwt(broken, now=_now())
    assert "not a key" not in str(error.value)
    assert is_github_app_reference("GitHub", "privateKey") and not is_github_app_reference("gitlab", "privateKey")
    assert not is_github_app_reference("github", "token")


@pytest.mark.asyncio
async def test_installation_tokens_are_discovered_scoped_to_the_repository_and_cached_until_expiry():
    clock_value = [_now()]
    fake = FakeGitHub(clock=lambda: clock_value[0])
    issuer = _issuer(fake, lambda: clock_value[0])
    credentials = GitHubAppCredentials(APP_ID, PRIVATE_KEY)

    first = await issuer.token_for(credentials, api_base_url=None, owner="acme", name="gitops")

    assert first.token == f"{INSTALLATION_TOKEN}1" and "ghs_" not in repr(first)
    assert first.installation_id == 77 and first.repository == "acme/gitops"
    assert (first.bot_name, first.bot_email) == (
        "greenkube-acme[bot]",
        "4242+greenkube-acme[bot]@users.noreply.github.com",
    )
    issue = next(request for request in fake.requests if request.method == "POST")
    assert json.loads(issue.content) == {"repositories": ["gitops"]}
    assert issue.headers["Authorization"].startswith("Bearer ey")  # the App JWT, never the token
    calls = len(fake.requests)

    # Cached until five minutes before expiry; renewed after.
    clock_value[0] += timedelta(minutes=50)
    assert (await issuer.token_for(credentials, api_base_url=None, owner="acme", name="gitops")) is first
    assert len(fake.requests) == calls
    clock_value[0] += timedelta(minutes=6)
    renewed = await issuer.token_for(credentials, api_base_url=None, owner="acme", name="gitops")
    assert renewed.token == f"{INSTALLATION_TOKEN}2"
    # The bot identity is cached per App.
    assert not any(request.url.path == "/app" for request in fake.requests[calls:])


@pytest.mark.asyncio
async def test_a_repository_outside_the_installation_fails_closed_without_secrets_in_the_error():
    fake = FakeGitHub(clock=_now, installed=False)
    issuer = _issuer(fake, _now)

    with pytest.raises(GitHubAppError) as error:
        await issuer.token_for(
            GitHubAppCredentials(APP_ID, PRIVATE_KEY), api_base_url=None, owner="acme", name="gitops"
        )

    assert "not installed" in str(error.value)
    assert "PRIVATE" not in str(error.value) and "Bearer" not in str(error.value)
    with pytest.raises(GitHubAppError):
        await issuer.token_for(GitHubAppCredentials(APP_ID, PRIVATE_KEY), api_base_url=None, owner="a/b", name="c")


@pytest.mark.asyncio
async def test_provider_outages_are_transient():
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    issuer = GitHubAppTokenIssuer(clock=_now, transport=httpx.MockTransport(unavailable))
    with pytest.raises(GitProviderTransientError):
        await issuer.token_for(GitHubAppCredentials(APP_ID, PRIVATE_KEY), api_base_url=None, owner="acme", name="g")


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str | None:
        self.reads.append(key)
        return self.values.get(key)


class _Providers:
    def __init__(self, config: GitOpsProviderConfig) -> None:
        self.config = config

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return self.config if provider_id == self.config.id else None

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return ["acme/gitops"]


_APP_PROVIDER = GitOpsProviderConfig(
    id="inferred-github.com",
    provider_type="github",
    secret_name="greenkube-git-credentials",
    secret_key="privateKey",
)


@pytest.mark.asyncio
async def test_credential_resolver_reads_the_app_id_next_to_the_private_key():
    secrets = _Secrets({"privateKey": PRIVATE_KEY, "appId": APP_ID})
    credentials = await GitProviderCredentialResolver(secrets).resolve_credentials(_APP_PROVIDER)

    assert isinstance(credentials, GitHubAppCredentials) and credentials.app_id == APP_ID
    assert secrets.reads == ["privateKey", "appId"]

    token_provider = GitOpsProviderConfig(id="t", provider_type="github", secret_name="s", secret_key="token")
    assert (
        await GitProviderCredentialResolver(_Secrets({"token": "ghp_x"})).resolve_credentials(token_provider) == "ghp_x"
    )
    with pytest.raises(GitProviderCredentialError):
        await GitProviderCredentialResolver(_Secrets({"privateKey": PRIVATE_KEY})).resolve_credentials(_APP_PROVIDER)


@pytest.mark.asyncio
async def test_registry_uses_a_repository_scoped_installation_token_and_the_bot_identity():
    fake = FakeGitHub(clock=_now)
    registry = GitProviderRegistry(
        cast(GitOpsProviderRepository, _Providers(_APP_PROVIDER)),
        _Secrets({"privateKey": PRIVATE_KEY, "appId": APP_ID}),
        app_tokens=_issuer(fake, _now),
    )
    repository = GitRepository(provider="github", owner="acme", name="gitops", default_branch="main")

    provider = await registry.get_provider("inferred-github.com", repository)
    try:
        inner: Any = provider._provider
        assert inner.token == f"{INSTALLATION_TOKEN}1"
        assert (inner.author_name, inner.author_email) == (
            "greenkube-acme[bot]",
            "4242+greenkube-acme[bot]@users.noreply.github.com",
        )
    finally:
        await provider.close()
