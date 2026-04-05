# tests/api/test_health_services.py
"""
Tests for the /api/v1/health/services endpoints.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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


class TestUpdateServiceConfig:
    """Tests for POST /api/v1/config/services."""

    def test_update_prometheus_url(self, client, monkeypatch):
        """Should update PROMETHEUS_URL and return fresh health check."""

        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.post(
                "/api/v1/config/services",
                json={"prometheus_url": "http://new-prometheus:9090"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "services" in data

    def test_update_opencost_url(self, client, monkeypatch):
        """Should update OPENCOST_API_URL and return fresh health check."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.post(
                "/api/v1/config/services",
                json={"opencost_url": "http://opencost:9003"},
            )

        assert response.status_code == 200

    def test_update_electricity_maps_token(self, client, monkeypatch):
        """Should update ELECTRICITY_MAPS_TOKEN and return fresh health check."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.post(
                "/api/v1/config/services",
                json={"electricity_maps_token": "new-token-abc123"},
            )

        assert response.status_code == 200

    def test_update_multiple_fields(self, client, monkeypatch):
        """Should accept multiple fields in a single request."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.post(
                "/api/v1/config/services",
                json={
                    "prometheus_url": "http://prom:9090",
                    "opencost_url": "http://oc:9003",
                    "boavizta_url": "https://custom-boavizta.example.com",
                },
            )

        assert response.status_code == 200

    def test_empty_update_returns_cached(self, client):
        """An empty body should still return health without errors."""
        with patch(
            "greenkube.api.routers.health.run_health_checks",
            new_callable=AsyncMock,
            return_value=_mock_health_response(),
        ):
            response = client.post("/api/v1/config/services", json={})

        assert response.status_code == 200
