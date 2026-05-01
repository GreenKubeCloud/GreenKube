# tests/collectors/discovery/test_opencost_discovery.py
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

from greenkube.collectors import discovery
from greenkube.collectors.discovery.opencost import OpenCostDiscovery


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
async def test_discover_opencost_by_name_and_port(monkeypatch):
    svc = make_svc("opencost-api", "opencost", [{"port": 8080, "name": "http"}])

    class MockCore:
        async def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda *a, **k: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda *a, **k: MockCore())

    # Patch ApiClient to be an async context manager yielding a dummy
    class MockApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr("greenkube.collectors.discovery.base.client.ApiClient", MockApiClient)

    async def mock_ensure_k8s_config():
        return True

    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", mock_ensure_k8s_config)

    url = await discovery.discover_service_dns("opencost")
    assert url is not None
    assert "opencost-api.opencost.svc.cluster.local" in url
    assert ":8080" in url


@pytest.mark.asyncio
async def test_discover_opencost_not_found(monkeypatch):
    svc = make_svc("some", "default", [{"port": 9090, "name": "http"}])

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

    url = await discovery.discover_service_dns("opencost")
    assert url is None


@pytest.mark.asyncio
async def test_opencost_prefers_9003_over_8080(monkeypatch):
    # Single service exposing both ports; discovery should pick 9003 per prefer_ports
    svc = make_svc(
        "opencost-api",
        "opencost",
        [{"port": 9003, "name": "http"}, {"port": 8080, "name": "http"}],
    )

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

    url = await discovery.discover_service_dns("opencost")
    assert url is not None
    assert ":9003" in url


@pytest.mark.asyncio
async def test_opencost_discover_returns_none_without_candidates():
    opencost_discovery = OpenCostDiscovery()
    opencost_discovery._collect_candidates = AsyncMock(return_value=[])

    assert await opencost_discovery.discover() is None


@pytest.mark.asyncio
async def test_opencost_discover_returns_verified_candidate():
    opencost_discovery = OpenCostDiscovery()
    opencost_discovery._collect_candidates = AsyncMock(return_value=[("http://opencost", 10)])
    opencost_discovery.probe_candidates = AsyncMock(return_value="http://opencost")

    assert await opencost_discovery.discover() == "http://opencost"


@pytest.mark.asyncio
async def test_opencost_discover_returns_none_when_probes_fail():
    opencost_discovery = OpenCostDiscovery()
    opencost_discovery._collect_candidates = AsyncMock(return_value=[("http://opencost", 10)])
    opencost_discovery.probe_candidates = AsyncMock(return_value=None)

    assert await opencost_discovery.discover() is None


class _FakeOpenCostResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeAsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.mark.asyncio
async def test_probe_opencost_endpoint_accepts_2xx_healthz_response():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_FakeOpenCostResponse(204))

    with patch(
        "greenkube.collectors.discovery.opencost.get_async_http_client",
        return_value=_FakeAsyncClientContext(client),
    ):
        assert await OpenCostDiscovery()._probe_opencost_endpoint("http://opencost/", 10) is True

    client.get.assert_awaited_once_with("http://opencost/healthz")


@pytest.mark.asyncio
async def test_probe_opencost_endpoint_rejects_non_2xx_healthz_response():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_FakeOpenCostResponse(503))

    with patch(
        "greenkube.collectors.discovery.opencost.get_async_http_client",
        return_value=_FakeAsyncClientContext(client),
    ):
        assert await OpenCostDiscovery()._probe_opencost_endpoint("http://opencost", 10) is False


@pytest.mark.asyncio
async def test_probe_opencost_endpoint_handles_request_and_unexpected_errors():
    for error in (httpx.RequestError("connection refused"), RuntimeError("boom")):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=error)

        with patch(
            "greenkube.collectors.discovery.opencost.get_async_http_client",
            return_value=_FakeAsyncClientContext(client),
        ):
            assert await OpenCostDiscovery()._probe_opencost_endpoint("http://opencost", 10) is False
