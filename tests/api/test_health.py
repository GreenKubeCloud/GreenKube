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

    def test_health_is_not_demo_by_default(self, client):
        """A real install never reports demo mode."""
        assert client.get("/api/v1/health").json()["demo"] is False

    def test_health_reports_demo_mode(self, client, monkeypatch):
        """`greenkube demo` sets GREENKUBE_DEMO_MODE, which the UI reads to show one demo notice."""
        from greenkube.core.config import get_config

        monkeypatch.setattr(get_config(), "GREENKUBE_DEMO_MODE", True)
        assert client.get("/api/v1/health").json()["demo"] is True


class TestSchemaReadiness:
    """F11: only the controller migrates; API replicas are not ready until the schema is current."""

    @staticmethod
    def _manager(*results):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        statuses = [
            result
            if isinstance(result, Exception)
            else SimpleNamespace(current=result, pending=(), expected_version=71)
            for result in results
        ]
        return SimpleNamespace(schema_status=AsyncMock(side_effect=statuses))

    def test_reports_migrating_until_the_controller_migrated(self, client, monkeypatch):
        manager = self._manager(False, True)
        monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: manager)
        client.app.state.check_schema = True

        first = client.get("/api/v1/health")
        second = client.get("/api/v1/health")

        assert first.status_code == 503
        assert first.json()["status"] == "migrating"
        assert second.status_code == 200
        assert second.json()["status"] == "ok"

    def test_reports_database_unavailable_without_crashing(self, client, monkeypatch):
        manager = self._manager(ConnectionError("down"))
        monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: manager)
        client.app.state.check_schema = True

        response = client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json()["status"] == "database_unavailable"

    def test_heartbeat_liveness_never_checks_the_schema(self, client, monkeypatch):
        manager = self._manager(False)
        monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: manager)
        client.app.state.check_schema = True

        assert client.get("/api/v1/health/heartbeat").status_code == 200
        manager.schema_status.assert_not_awaited()
