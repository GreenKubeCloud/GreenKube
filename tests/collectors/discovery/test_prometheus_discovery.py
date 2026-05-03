# tests/collectors/discovery/test_prometheus_discovery.py
"""
TDD tests for service discovery helpers.

We will mock the Kubernetes client CoreV1Api and its
list_service_for_all_namespaces() return value to simulate services
existing in different namespaces.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from greenkube.collectors.discovery.prometheus import PrometheusDiscovery


def make_svc(name, namespace, ports, labels=None):
    """Helper to build a fake V1Service-like object for tests."""
    svc = SimpleNamespace()
    svc.metadata = SimpleNamespace()
    svc.metadata.name = name
    svc.metadata.namespace = namespace
    svc.spec = SimpleNamespace()
    svc.spec.ports = []
    for p in ports:
        port_obj = SimpleNamespace()
        port_obj.port = p.get("port")
        port_obj.name = p.get("name")
        svc.spec.ports.append(port_obj)
    svc.metadata.labels = labels or {}
    return svc


@pytest.mark.asyncio
async def test_discover_prometheus_by_name(monkeypatch):
    from greenkube.collectors import discovery

    # Mock CoreV1Api to return a list with a prometheus service
    svc1 = make_svc("prometheus-k8s", "monitoring", [{"port": 9090, "name": "http"}])
    svc2 = make_svc("some-other", "default", [{"port": 8080, "name": "http"}])

    class MockCore:
        async def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc1, svc2])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda *a, **k: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda *a, **k: MockCore())

    class MockApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr("greenkube.collectors.discovery.base.client.ApiClient", MockApiClient)

    async def mock_ensure_k8s_config():
        return True

    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", mock_ensure_k8s_config)

    # Mock probes to avoid HTTP calls (though BaseDiscovery skips them in tests)
    # We rely on BaseDiscovery bypassing probes in tests when PYTEST_CURRENT_TEST is set.

    url = await discovery.discover_service_dns("prometheus")
    assert url is not None
    assert "prometheus-k8s.monitoring.svc.cluster.local" in url
    assert ":9090" in url


@pytest.mark.asyncio
async def test_discover_prometheus_not_found(monkeypatch):
    from greenkube.collectors import discovery

    svc = make_svc("other", "default", [{"port": 8080, "name": "http"}])

    class MockCore:
        async def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda *a, **k: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda *a, **k: MockCore())

    class MockApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr("greenkube.collectors.discovery.base.client.ApiClient", MockApiClient)

    async def mock_ensure_k8s_config():
        return True

    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", mock_ensure_k8s_config)

    url = await discovery.discover_service_dns("prometheus")
    assert url is None


@pytest.mark.asyncio
async def test_prometheus_prefers_namespace_and_labels(monkeypatch):
    from greenkube.collectors import discovery

    # service without preferred labels in default namespace
    svc_default = make_svc("prometheus", "default", [{"port": 9090, "name": "http"}])
    # service in monitoring namespace with preferred labels
    svc_monitor = make_svc(
        "prometheus-k8s",
        "monitoring",
        [{"port": 9090, "name": "http"}],
        labels={"app.kubernetes.io/name": "prometheus", "app.kubernetes.io/instance": "k8s"},
    )

    class MockCore:
        async def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc_default, svc_monitor])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda *a, **k: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda *a, **k: MockCore())

    class MockApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr("greenkube.collectors.discovery.base.client.ApiClient", MockApiClient)

    async def mock_ensure_k8s_config():
        return True

    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", mock_ensure_k8s_config)

    url = await discovery.discover_service_dns("prometheus")
    assert url is not None
    # should pick the monitoring one with labels
    assert "prometheus-k8s.monitoring.svc.cluster.local" in url
    assert ":9090" in url


@pytest.mark.asyncio
async def test_prometheus_discover_returns_none_without_candidates():
    discovery = PrometheusDiscovery()
    discovery._collect_candidates = AsyncMock(return_value=[])

    assert await discovery.discover() is None


@pytest.mark.asyncio
async def test_prometheus_discover_returns_verified_candidate():
    discovery = PrometheusDiscovery()
    discovery._collect_candidates = AsyncMock(return_value=[("http://prometheus", 10)])
    discovery.probe_candidates = AsyncMock(return_value="http://prometheus")

    assert await discovery.discover() == "http://prometheus"


@pytest.mark.asyncio
async def test_prometheus_discover_returns_none_when_probes_fail():
    discovery = PrometheusDiscovery()
    discovery._collect_candidates = AsyncMock(return_value=[("http://prometheus", 10)])
    discovery.probe_candidates = AsyncMock(return_value=None)

    assert await discovery.discover() is None


class _FakePrometheusResponse:
    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class _FakeAsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.mark.asyncio
async def test_probe_prometheus_endpoint_accepts_successful_query_response():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_FakePrometheusResponse(200, {"status": "success"}))

    with patch(
        "greenkube.collectors.discovery.prometheus.get_async_http_client",
        return_value=_FakeAsyncClientContext(client),
    ):
        assert await PrometheusDiscovery()._probe_prometheus_endpoint("http://prometheus/", 10) is True

    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_prometheus_endpoint_tries_all_paths_before_failure():
    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[
            _FakePrometheusResponse(500, {"status": "error"}),
            _FakePrometheusResponse(200, json_error=True),
            httpx.RequestError("connection refused"),
        ]
    )

    with patch(
        "greenkube.collectors.discovery.prometheus.get_async_http_client",
        return_value=_FakeAsyncClientContext(client),
    ):
        assert await PrometheusDiscovery()._probe_prometheus_endpoint("http://prometheus", 10) is False

    assert client.get.await_count == 3


@pytest.mark.asyncio
async def test_probe_prometheus_endpoint_handles_unexpected_errors():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(
        "greenkube.collectors.discovery.prometheus.get_async_http_client",
        return_value=_FakeAsyncClientContext(client),
    ):
        assert await PrometheusDiscovery()._probe_prometheus_endpoint("http://prometheus", 10) is False
