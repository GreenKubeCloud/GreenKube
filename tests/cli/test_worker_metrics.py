import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from greenkube.cli.worker_metrics import create_worker_metrics_app, create_worker_metrics_server
from greenkube.core.observability import record_sync_outcome


def _configure_metrics_auth(monkeypatch, *, api_key: str, environment: str):
    config = SimpleNamespace(API_KEY=api_key, ENVIRONMENT=environment, API_AUTH_MODE="api_key")
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: config)
    monkeypatch.setattr("greenkube.core.config.config", config)


def test_worker_metrics_requires_existing_api_key_and_exposes_control_registry(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="worker-secret", environment="test")
    record_sync_outcome("synced")

    with TestClient(create_worker_metrics_app()) as client:
        assert client.get("/prometheus/metrics").status_code == 401
        assert client.get("/docs").status_code == 404
        assert (
            client.get(
                "/prometheus/metrics",
                headers={"Authorization": "Bearer wrong-secret"},
            ).status_code
            == 401
        )
        response = client.get(
            "/prometheus/metrics",
            headers={"Authorization": "Bearer worker-secret"},
        )

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert 'greenkube_gitops_sync_outcomes_total{outcome="synced"}' in response.text


def test_worker_metrics_fail_closed_in_production_without_api_key(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="", environment="production")

    with TestClient(create_worker_metrics_app()) as client:
        response = client.get("/prometheus/metrics")

    assert response.status_code == 503


def test_worker_metrics_keep_development_api_key_behavior(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="", environment="development")

    with TestClient(create_worker_metrics_app()) as client:
        response = client.get("/prometheus/metrics")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_embedded_worker_listener_scrapes_the_same_authenticated_registry(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="worker-secret", environment="test")
    record_sync_outcome("timeout")
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]

    server = create_worker_metrics_server(port)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get("/prometheus/metrics")).status_code == 401
            response = await client.get(
                "/prometheus/metrics",
                headers={"Authorization": "Bearer worker-secret"},
            )
    finally:
        server.should_exit = True
        await task

    assert response.status_code == 200
    assert 'greenkube_gitops_sync_outcomes_total{outcome="timeout"}' in response.text
