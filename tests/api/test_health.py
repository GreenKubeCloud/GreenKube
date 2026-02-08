# tests/api/test_health.py
"""Tests for the health endpoint."""


class TestHealthEndpoint:
    """Tests for GET /api/v1/health."""

    def test_health_returns_200(self, client):
        """Health endpoint should return 200 with status ok."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200

    def test_health_returns_status_ok(self, client):
        """Health endpoint should return a JSON body with status 'ok'."""
        response = client.get("/api/v1/health")
        data = response.json()
        assert data["status"] == "ok"

    def test_health_returns_version(self, client):
        """Health endpoint should include the app version."""
        response = client.get("/api/v1/health")
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)
