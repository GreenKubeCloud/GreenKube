"""Controller Prometheus endpoint (roadmap A12, BUG-14)."""

import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from greenkube.cli.controller_metrics import (
    create_controller_metrics_app,
    create_controller_metrics_server,
    start_controller_metrics_server,
    stop_controller_metrics_server,
)
from greenkube.core.observability import (
    record_collection_request,
    record_collection_window,
    record_metric_provenance,
    record_verification,
)
from greenkube.models.metrics import CombinedMetric


def _configure_metrics_auth(monkeypatch, *, api_key: str, environment: str = "test"):
    config = SimpleNamespace(API_KEY=api_key, ENVIRONMENT=environment, API_AUTH_MODE="api_key")
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: config)
    monkeypatch.setattr("greenkube.core.config.config", config)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_controller_endpoint_exposes_controller_side_telemetry(monkeypatch):
    """Verification and collection telemetry recorded in the controller is scrapeable (BUG-14)."""
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    record_verification("passed")
    record_collection_request("opencost", "error", 0.2)
    record_collection_window(3.5)
    record_metric_provenance(
        [
            CombinedMetric(pod_name="p", namespace="n", cost_source="opencost", intensity_source="zone_default"),
            CombinedMetric(pod_name="p", namespace="n", container_name="c", usage_source="prometheus"),
        ]
    )

    with TestClient(create_controller_metrics_app()) as client:
        assert client.get("/prometheus/metrics").status_code == 401
        assert client.get("/prometheus/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/docs", headers={"Authorization": "Bearer controller-secret"}).status_code == 404
        response = client.get("/prometheus/metrics", headers={"Authorization": "Bearer controller-secret"})

    assert response.status_code == 200
    body = response.text
    assert 'greenkube_verification_outcomes_total{outcome="passed"}' in body
    assert 'greenkube_collection_source_requests_total{outcome="error",source="opencost"}' in body
    assert "greenkube_collection_window_duration_seconds_count" in body
    assert 'greenkube_metric_rows_written_total{dimension="cost",scope="pod",source="opencost"}' in body
    assert 'greenkube_metric_rows_written_total{dimension="intensity",scope="pod",source="zone_default"}' in body
    assert 'greenkube_metric_rows_written_total{dimension="usage",scope="container",source="prometheus"}' in body


def test_controller_endpoint_serves_the_pod_network_when_the_controller_has_no_api_key(monkeypatch):
    """Helm keeps the API key out of the controller (least privilege); the endpoint stays scrapeable."""
    _configure_metrics_auth(monkeypatch, api_key="", environment="production")

    with TestClient(create_controller_metrics_app()) as client:
        response = client.get("/prometheus/metrics")
        assert client.get("/api/v1/health").status_code == 404

    assert response.status_code == 200


def test_controller_endpoint_also_answers_on_the_default_metrics_path(monkeypatch):
    """Scrapers that assume Prometheus' default ``/metrics`` path get the same registry, with the same auth."""
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    record_collection_window(1.0)

    with TestClient(create_controller_metrics_app()) as client:
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers={"Authorization": "Bearer controller-secret"})

    assert response.status_code == 200
    assert "greenkube_collection_window_duration_seconds_count" in response.text


def test_controller_metrics_port_must_be_valid():
    with pytest.raises(ValueError):
        create_controller_metrics_server(0)
    with pytest.raises(ValueError):
        create_controller_metrics_server(70_000)


async def test_disabled_port_starts_nothing():
    assert await start_controller_metrics_server(0) is None
    await stop_controller_metrics_server(None)


async def test_listener_serves_and_stops(monkeypatch):
    _configure_metrics_auth(monkeypatch, api_key="controller-secret")
    port = _free_port()

    handle = await start_controller_metrics_server(port, host="127.0.0.1")
    assert handle is not None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/prometheus/metrics",
                headers={"Authorization": "Bearer controller-secret"},
            )
        assert response.status_code == 200
    finally:
        await stop_controller_metrics_server(handle)
    assert handle.task.done()


async def test_bind_failure_does_not_stop_the_controller():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        handle = await start_controller_metrics_server(port, host="127.0.0.1", startup_timeout=5)

    assert handle is None


def test_provenance_mix_bounds_unknown_values():
    record_metric_provenance([CombinedMetric(pod_name="p", namespace="n", cost_source="something-new")])

    from greenkube.core.observability import generate_latest_observability

    assert 'source="something-new"' not in generate_latest_observability().decode()
