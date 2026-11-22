# tests/api/test_cors_policy.py
"""Regression tests for BUG-59 (roadmap N4): CORS defaults and credentialed origins.

The API is same-origin by default. A wildcard origin must never be combined
with ``Access-Control-Allow-Credentials: true``: once the UI uses cookie
sessions, that combination would let any website act as the signed-in user.
"""

import pytest
from fastapi.testclient import TestClient

from greenkube.api import app as app_module
from greenkube.core.config import Config

ATTACKER_ORIGIN = "https://attacker.example"
TRUSTED_ORIGIN = "https://greenkube.example.com"


def _client(monkeypatch, cors_origins: str | None) -> TestClient:
    if cors_origins is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", cors_origins)
    cfg = Config(_env_file=None)
    monkeypatch.setattr(app_module, "get_config", lambda: cfg)
    return TestClient(app_module.create_app())


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/api/v1/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_cors_origins_default_is_empty():
    assert Config.model_fields["CORS_ORIGINS"].default == ""


def test_default_policy_is_same_origin(monkeypatch):
    client = _client(monkeypatch, None)

    simple = client.get("/api/v1/health", headers={"Origin": ATTACKER_ORIGIN, "Cookie": "greenkube_session=x"})
    preflight = _preflight(client, ATTACKER_ORIGIN)

    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-credentials" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("cors_origins", ["*", " * ", f"{TRUSTED_ORIGIN},*"])
def test_wildcard_origin_never_allows_credentials(monkeypatch, cors_origins):
    client = _client(monkeypatch, cors_origins)

    simple = client.get("/api/v1/health", headers={"Origin": ATTACKER_ORIGIN, "Cookie": "greenkube_session=x"})
    preflight = _preflight(client, ATTACKER_ORIGIN)

    for response in (simple, preflight):
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in response.headers


def test_explicit_origin_is_reflected_with_credentials(monkeypatch):
    client = _client(monkeypatch, f"{TRUSTED_ORIGIN}, https://other.example.com")

    trusted = _preflight(client, TRUSTED_ORIGIN)
    attacker = _preflight(client, ATTACKER_ORIGIN)

    assert trusted.headers.get("access-control-allow-origin") == TRUSTED_ORIGIN
    assert trusted.headers.get("access-control-allow-credentials") == "true"
    assert "access-control-allow-origin" not in attacker.headers
