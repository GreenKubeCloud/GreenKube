"""Strict-Transport-Security on HTTPS responses (production-readiness security hardening)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from greenkube.core.config import get_config


def _client(monkeypatch, *, base_url: str = "http://testserver", client=("testclient", 50000), **env) -> TestClient:
    import greenkube.api.app as app_module

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_config().reload()
    return TestClient(app_module.create_app(), base_url=base_url, client=client)


def test_hsts_is_sent_on_https_responses(monkeypatch):
    client = _client(monkeypatch, base_url="https://testserver")

    response = client.get("/api/v1/health")

    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_hsts_is_not_sent_over_plain_http(monkeypatch):
    client = _client(monkeypatch)

    assert "strict-transport-security" not in client.get("/api/v1/health").headers


def test_hsts_follows_forwarded_proto_from_a_trusted_proxy_only(monkeypatch):
    trusted = _client(monkeypatch, client=("10.0.0.5", 50000), TRUSTED_PROXY_IPS="10.0.0.0/8")
    assert "strict-transport-security" in trusted.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers

    untrusted = _client(monkeypatch, client=("198.51.100.4", 50000), TRUSTED_PROXY_IPS="10.0.0.0/8")
    assert (
        "strict-transport-security"
        not in untrusted.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers
    )


def test_hsts_is_sent_when_the_deployment_declares_https(monkeypatch):
    """SESSION_COOKIE_SECURE=always (an Ingress with TLS): browsers ignore HSTS over plain HTTP anyway."""
    client = _client(monkeypatch, SESSION_COOKIE_SECURE="always")

    assert client.get("/api/v1/health").headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize(
    ("max_age", "subdomains", "expected"),
    [("0", "false", None), ("600", "true", "max-age=600; includeSubDomains")],
)
def test_hsts_is_configurable(monkeypatch, max_age, subdomains, expected):
    client = _client(
        monkeypatch, base_url="https://testserver", HSTS_MAX_AGE=max_age, HSTS_INCLUDE_SUBDOMAINS=subdomains
    )

    assert client.get("/api/v1/health").headers.get("strict-transport-security") == expected
