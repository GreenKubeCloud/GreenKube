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


def test_dynamic_path_parameters_share_a_route_rate_limit(monkeypatch):
    app = _create_app_with_limit(monkeypatch, "1/minute")
    client = TestClient(app)

    first = client.get("/api/v1/health/services/prometheus")
    second = client.get("/api/v1/health/services/opencost")

    assert first.status_code != 429
    assert second.status_code == 429


def test_routes_of_included_routers_have_separate_buckets(monkeypatch):
    """Every route of an included router used to share one '<matched-route>' bucket."""
    app = _create_app_with_limit(monkeypatch, "1/minute")
    client = TestClient(app)

    assert client.get("/api/v1/health/services/prometheus").status_code != 429
    # Other routes of other routers keep their own budget.
    assert client.get("/api/v1/version").status_code != 429
    assert client.get("/api/v1/health").status_code != 429


def test_route_key_is_the_registered_template(monkeypatch):
    from starlette.requests import Request

    from greenkube.api.app import RateLimitMiddleware

    app = _create_app_with_limit(monkeypatch, "5/minute")

    def key(method: str, path: str) -> str:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "root_path": "",
            "headers": [],
            "query_string": b"",
            "app": app,
        }
        return RateLimitMiddleware._route_key(Request(scope))

    assert key("POST", "/api/v1/automation/plans/abc/approve") == "/api/v1/automation/plans/{plan_id}/approve"
    assert key("GET", "/api/v1/health/services/opencost") == key("GET", "/api/v1/health/services/prometheus")
    assert key("GET", "/api/v1/health") != key("GET", "/api/v1/version")
    assert key("GET", "/api/v1/does-not-exist") == "<unmatched-route>"


def test_spoofed_forwarded_for_does_not_choose_the_rate_limit_client(monkeypatch):
    """Behind a trusted proxy the rightmost untrusted hop is the client, never the leftmost entry."""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    app = _create_app_with_limit(monkeypatch, "1/minute")
    client = TestClient(app, client=("10.0.0.5", 50000))

    first = client.get("/api/v1/health", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    second = client.get("/api/v1/health", headers={"X-Forwarded-For": "2.2.2.2, 203.0.113.7"})
    other = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.8"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert other.status_code == 200


def test_forwarded_for_from_an_untrusted_peer_is_ignored(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    app = _create_app_with_limit(monkeypatch, "1/minute")
    client = TestClient(app, client=("198.51.100.4", 50000))

    assert client.get("/api/v1/health", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/api/v1/health", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429
