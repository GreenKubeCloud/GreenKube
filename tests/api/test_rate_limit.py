"""Tests for API rate limiting enforcement."""

from fastapi.testclient import TestClient

from greenkube.core.config import get_config


def _create_app_with_limit(monkeypatch, limit: str):
    import greenkube.api.app as app_module

    monkeypatch.setenv("API_RATE_LIMIT", limit)
    get_config().reload()
    return app_module.create_app()


def test_requests_above_the_limit_return_429(monkeypatch):
    app = _create_app_with_limit(monkeypatch, "3/minute")
    client = TestClient(app)

    statuses = [client.get("/api/v1/health").status_code for _ in range(4)]

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429


def test_rate_limit_response_has_detail(monkeypatch):
    app = _create_app_with_limit(monkeypatch, "1/minute")
    client = TestClient(app)

    assert client.get("/api/v1/health").status_code == 200
    response = client.get("/api/v1/health")

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]
