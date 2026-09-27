"""Behaviour and security tests for the SPA catch-all routes."""

import pytest
from fastapi.testclient import TestClient

import greenkube.api.app as app_module


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>spa-index</html>")
    (frontend / "_app").mkdir()
    (frontend / "_app" / "app.js").write_text("console.log('ok')")
    (tmp_path / "secret.txt").write_text("top-secret")

    monkeypatch.setattr(app_module, "FRONTEND_DIR", frontend)
    return TestClient(app_module.create_app())


def test_spa_serves_index_for_client_routes(spa_client):
    response = spa_client.get("/some/client/route")
    assert response.status_code == 200
    assert "spa-index" in response.text


def test_spa_serves_hashed_static_asset(spa_client):
    response = spa_client.get("/_app/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_spa_rejects_single_level_traversal(spa_client):
    response = spa_client.get("/%2e%2e/secret.txt")
    assert response.status_code == 404
    assert "top-secret" not in response.text


def test_spa_rejects_deep_traversal(spa_client):
    response = spa_client.get("/%2e%2e/%2e%2e/etc/passwd")
    assert response.status_code == 404


def test_spa_rejects_encoded_backslash_traversal(spa_client):
    response = spa_client.get("/..%2fsecret.txt")
    assert response.status_code == 404
    assert "top-secret" not in response.text


def test_unknown_api_route_returns_json_404(spa_client):
    response = spa_client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_proxy_paths_return_404(spa_client):
    response = spa_client.get("/oauth2/callback")
    assert response.status_code == 404
