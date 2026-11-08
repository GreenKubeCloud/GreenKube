# tests/api/test_health_services.py
"""
Tests for the /api/v1/health/services endpoints.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from greenkube.models.health import (
    HealthCheckResponse,
    ServiceHealth,
    ServiceStatus,
)


def _mock_health_response():
    """Create a mock HealthCheckResponse for tests."""
    return HealthCheckResponse(
        status="degraded",
        version="0.2.6",
        services={
            "prometheus": ServiceHealth(
                name="prometheus",
                status=ServiceStatus.HEALTHY,
                url="http://prometheus:9090",
                message="Prometheus is reachable and responding.",
                latency_ms=12.5,
                last_check=datetime.now(timezone.utc),
                configured=True,
            ),
            "opencost": ServiceHealth(
                name="opencost",
                status=ServiceStatus.UNCONFIGURED,
                message="OpenCost URL is not configured and service discovery failed.",
                last_check=datetime.now(timezone.utc),
            ),
            "electricity_maps": ServiceHealth(
                name="electricity_maps",
                status=ServiceStatus.UNCONFIGURED,
                message="Electricity Maps API token is not set.",
                last_check=datetime.now(timezone.utc),
            ),
            "boavizta": ServiceHealth(
                name="boavizta",
                status=ServiceStatus.HEALTHY,
                url="https://api.boavizta.org",
                message="Boavizta API is reachable.",
                latency_ms=45.0,
                last_check=datetime.now(timezone.utc),
                configured=True,
            ),
            "kubernetes": ServiceHealth(
                name="kubernetes",
                status=ServiceStatus.UNREACHABLE,
                message="Cannot reach Kubernetes API.",
                last_check=datetime.now(timezone.utc),
            ),
        },
    )


def _allow_service_hosts(monkeypatch, *hosts: str) -> None:
    from greenkube.core.config import config

    monkeypatch.setenv("SERVICE_URL_ALLOWLIST", ",".join(hosts))
    config.reload()


class TestGetServicesHealth:
    """Tests for GET /api/v1/health/services."""

    def test_returns_200(self, client):
        """Endpoint should return 200."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.get("/api/v1/health/services")
        assert response.status_code == 200

    def test_returns_all_services(self, client):
        """Response should include all 5 services."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.get("/api/v1/health/services")

        data = response.json()
        assert "services" in data
        services = data["services"]
        assert "prometheus" in services
        assert "opencost" in services
        assert "electricity_maps" in services
        assert "boavizta" in services
        assert "kubernetes" in services

    def test_service_has_expected_fields(self, client):
        """Each service should have name, status, message, url fields."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.get("/api/v1/health/services")

        data = response.json()
        prom = data["services"]["prometheus"]
        assert prom["name"] == "prometheus"
        assert prom["status"] == "healthy"
        assert prom["url"] == "http://prometheus:9090"
        assert prom["latency_ms"] is not None

    def test_force_param(self, client):
        """Passing ?force=true should be accepted."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ) as mock_run:
            response = client.get("/api/v1/health/services?force=true")

        assert response.status_code == 200
        mock_run.assert_called_once_with(force=True)


class TestGetSingleServiceHealth:
    """Tests for GET /api/v1/health/services/{service_name}."""

    def test_known_service(self, client):
        """Should return 200 for a known service."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.get("/api/v1/health/services/prometheus")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "prometheus"
        assert data["status"] == "healthy"

    def test_unknown_service_returns_404(self, client):
        """Should return 404 for an unknown service name."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.get("/api/v1/health/services/nonexistent")

        assert response.status_code == 404


class TestServiceConfigIsConfigurationAsCode:
    """A13, BUG-11, BUG-60: the API no longer edits data sources at runtime."""

    @pytest.mark.parametrize(
        "body",
        [
            {"prometheus_url": "http://new-prometheus:9090"},
            {"electricity_maps_token": "new-token"},
            {"wattnet_email": "user@example.com", "wattnet_password": "secret"},
            {},
        ],
    )
    def test_runtime_service_updates_are_gone(self, client, monkeypatch, body):
        monkeypatch.setenv("PROMETHEUS_URL", "http://original-prometheus:9090")
        from greenkube.core.config import get_config

        get_config().reload()

        response = client.post("/api/v1/config/services", json=body)

        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "CONFIGURATION_AS_CODE"
        assert get_config().PROMETHEUS_URL == "http://original-prometheus:9090"

    def test_api_has_no_secret_writer(self):
        import greenkube.core.k8s_secret_store as store

        assert not hasattr(store, "patch_k8s_secret")

    def test_endpoint_is_not_documented(self, client):
        schema = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/config/services" not in schema["paths"]
