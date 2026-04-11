# tests/api/test_security.py
"""
Tests for API security hardening: headers, CORS, rate limiting, auth.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)


class TestSecurityHeaders:
    """Verify that all OWASP-recommended security headers are present."""

    def test_x_content_type_options(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_x_xss_protection(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_referrer_policy(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy(self, client):
        resp = client.get("/api/v1/health")
        policy = resp.headers.get("Permissions-Policy")
        assert "camera=()" in policy
        assert "microphone=()" in policy

    def test_content_security_policy(self, client):
        resp = client.get("/api/v1/health")
        csp = resp.headers.get("Content-Security-Policy")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_cache_control(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_security_headers_on_api_docs(self, client):
        resp = client.get("/api/v1/docs")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"


class TestCORSPolicy:
    """Verify that CORS is not overly permissive."""

    def test_cors_does_not_allow_all_methods(self, client):
        """CORS should not use allow_methods=* (we restrict to GET, POST, OPTIONS)."""
        resp = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        allowed = resp.headers.get("Access-Control-Allow-Methods", "")
        assert "DELETE" not in allowed

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
