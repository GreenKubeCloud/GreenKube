# tests/api/test_security.py
"""
Tests for API security hardening: headers, CORS, rate limiting, auth.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_authenticated_actor,
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    validate_service_url,
    verify_api_key,
)

API_KEY = "test-secret-key"


@contextmanager
def _secured_app(monkeypatch):
    """Yield a TestClient for an app configured with an API key."""
    monkeypatch.setenv("GREENKUBE_API_KEY", API_KEY)
    from greenkube.core.config import Config

    test_cfg = Config()
    with patch("greenkube.core.config.config", test_cfg):
        with patch("greenkube.core.config.get_config", return_value=test_cfg):
            yield TestClient(create_app())


class TestSecurityHeaders:
    """Verify that all OWASP-recommended security headers are present.

    Document-only headers (X-Frame-Options, X-XSS-Protection, CSP,
    Permissions-Policy, X-Robots-Tag) are only set on HTML responses (the
    SPA entry point), not on JSON API responses.  This avoids bloat on
    API responses that can produce malformed responses when a reverse proxy
    merges headers from auth subrequests and backend responses.
    """

    def test_common_headers_on_api(self, client):
        """Nosniff and Referrer-Policy must be on all responses."""
        resp = client.get("/api/v1/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_document_headers_not_on_api(self, client):
        """Document-only headers must NOT appear on JSON API responses."""
        resp = client.get("/api/v1/health")
        assert "X-Frame-Options" not in resp.headers
        assert "Content-Security-Policy" not in resp.headers
        assert "Permissions-Policy" not in resp.headers
        assert "X-Robots-Tag" not in resp.headers

    def test_cache_control(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("Cache-Control") == "no-cache"

    def test_security_headers_on_api_docs(self, client):
        resp = client.get("/api/v1/docs")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_spa_csp_allows_the_configured_google_fonts_stylesheet(self, monkeypatch, tmp_path):
        from greenkube.api import app as app_module

        (tmp_path / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
        monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path)

        with _secured_app(monkeypatch) as c:
            resp = c.get("/")

        assert resp.status_code == 200
        style_src = next(
            directive for directive in resp.headers["Content-Security-Policy"].split(";") if "style-src" in directive
        )
        assert "https://fonts.googleapis.com" in style_src
        assert "https:" not in style_src.replace("https://fonts.googleapis.com", "")


class TestCORSPolicy:
    """Verify that CORS is not overly permissive."""

    def test_cors_does_not_allow_all_methods(self, client):
        """CORS should not use allow_methods=* (we restrict to GET, POST, PATCH, DELETE, OPTIONS)."""
        resp = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "PUT",
            },
        )
        allowed = resp.headers.get("Access-Control-Allow-Methods", "")
        assert "PUT" not in allowed

    def test_cors_does_not_allow_all_headers(self, client):
        """CORS should only allow Authorization and Content-Type headers."""
        resp = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Custom-Evil",
            },
        )
        allowed = resp.headers.get("Access-Control-Allow-Headers", "")
        assert "X-Custom-Evil" not in allowed


class TestAPIKeyAuth:
    """Verify that API key authentication works correctly."""

    def test_production_api_requires_api_key(self, monkeypatch):
        from greenkube.api import app as app_module
        from greenkube.core.config import Config

        cfg = Config(ENVIRONMENT="production", API_AUTH_MODE="api_key", GREENKUBE_API_KEY="")
        monkeypatch.setattr(app_module, "get_config", lambda: cfg)

        with pytest.raises(ValueError, match="GREENKUBE_API_KEY must be configured in production"):
            app_module.create_app()

    def test_missing_api_key_returns_401(
        self, mock_carbon_repo, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo
    ):
        """When GREENKUBE_API_KEY is set, requests without a key get 401."""
        with patch.dict("os.environ", {"GREENKUBE_API_KEY": "test-secret-key"}, clear=False):
            from greenkube.core.config import Config

            test_cfg = Config()
            with patch("greenkube.core.config.config", test_cfg):
                with patch("greenkube.core.config.get_config", return_value=test_cfg):
                    app = create_app()
                    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
                    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
                    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
                    app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

                    with TestClient(app) as c:
                        resp = c.get("/api/v1/metrics/summary")
                        assert resp.status_code == 401

                    app.dependency_overrides.clear()

    def test_health_exempt_from_api_key(
        self, mock_carbon_repo, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo
    ):
        """Health endpoint should be accessible even when API key is set."""
        with patch.dict("os.environ", {"GREENKUBE_API_KEY": "test-secret-key"}, clear=False):
            from greenkube.core.config import Config

            test_cfg = Config()
            with patch("greenkube.core.config.config", test_cfg):
                with patch("greenkube.core.config.get_config", return_value=test_cfg):
                    app = create_app()
                    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
                    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
                    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
                    app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

                    with TestClient(app) as c:
                        resp = c.get("/api/v1/health")
                        assert resp.status_code == 200

                    app.dependency_overrides.clear()

    def test_valid_bearer_key_is_accepted(self, monkeypatch):
        """A correct `Authorization: Bearer <key>` header is accepted."""
        with _secured_app(monkeypatch) as c:
            resp = c.get("/api/v1/version", headers={"Authorization": f"Bearer {API_KEY}"})
        assert resp.status_code == 200

    def test_raw_key_without_bearer_is_rejected(self, monkeypatch):
        """The key must be sent with the Bearer scheme."""
        with _secured_app(monkeypatch) as c:
            resp = c.get("/api/v1/version", headers={"Authorization": API_KEY})
        assert resp.status_code == 401

    def test_wrong_scheme_is_rejected(self, monkeypatch):
        with _secured_app(monkeypatch) as c:
            resp = c.get("/api/v1/version", headers={"Authorization": f"Token {API_KEY}"})
        assert resp.status_code == 401

    def test_health_services_requires_api_key(self, monkeypatch):
        """The health-services endpoint leaks URLs/errors and must not be public."""
        with _secured_app(monkeypatch) as c:
            resp = c.get("/api/v1/health/services")
        assert resp.status_code == 401

    def test_prometheus_metrics_requires_api_key(self, monkeypatch):
        """The Prometheus exposition endpoint must be protected when a key is set."""
        with _secured_app(monkeypatch) as c:
            assert c.get("/prometheus/metrics").status_code == 401
            assert c.get("/prometheus/metrics", headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200

    @pytest.mark.parametrize(
        ("path", "root_path"),
        [
            ("/greenkube/api/v1/config", "/greenkube"),
            ("/api/v1/config", "/greenkube"),
            ("/greenkube/prometheus/metrics", "/greenkube"),
        ],
    )
    def test_root_path_never_bypasses_api_key(self, monkeypatch, path, root_path):
        with _secured_app(monkeypatch):
            request = Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode(),
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "root_path": root_path,
                    "app": None,
                }
            )
            with pytest.raises(HTTPException) as error:
                verify_api_key(request)

        assert error.value.status_code == 401

    def test_root_path_keeps_health_exemption_exact(self, monkeypatch):
        with _secured_app(monkeypatch):
            request = Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/greenkube/api/v1/health",
                    "raw_path": b"/greenkube/api/v1/health",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "root_path": "/greenkube",
                    "app": None,
                }
            )
            verify_api_key(request)

    def test_authenticated_actor_is_derived_from_the_bearer_key(self, monkeypatch):
        with _secured_app(monkeypatch):
            request = Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/api/v1/version",
                    "raw_path": b"/api/v1/version",
                    "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {API_KEY}".encode())],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "root_path": "",
                    "app": None,
                }
            )
            verify_api_key(request)
            actor = get_authenticated_actor(request)

        assert actor.startswith("api-key:")
        assert API_KEY not in actor


class TestInputValidation:
    """Verify that invalid inputs are rejected."""

    def test_invalid_namespace_rejected(self, client):
        """Namespace with SQL injection attempt should be rejected."""
        resp = client.get("/api/v1/metrics/summary?namespace='; DROP TABLE metrics;--")
        assert resp.status_code == 400

    def test_valid_namespace_accepted(self, client):
        resp = client.get("/api/v1/metrics/summary?namespace=kube-system")
        assert resp.status_code == 200

    def test_namespace_too_long_rejected(self, client):
        resp = client.get(f"/api/v1/metrics/summary?namespace={'a' * 64}")
        assert resp.status_code == 400

    def test_metadata_service_host_rejected(self):
        with pytest.raises(HTTPException):
            validate_service_url("http://metadata.google.internal/latest/meta-data")

    def test_service_url_host_must_be_configured_or_allowlisted(self):
        from greenkube.core.config import Config

        config = Config(
            PROMETHEUS_URL="http://prometheus:9090",
            SERVICE_URL_ALLOWLIST="api.example.test",
        )
        with patch("greenkube.core.config.get_config", return_value=config):
            assert validate_service_url("http://prometheus:9090/api/v1/query") == "http://prometheus:9090/api/v1/query"
            assert validate_service_url("https://api.example.test/v1") == "https://api.example.test/v1"
            with pytest.raises(HTTPException):
                validate_service_url("https://untrusted.example.test")

    def test_embedded_credentials_rejected(self):
        with pytest.raises(HTTPException):
            validate_service_url("http://user:password@prometheus:9090")
