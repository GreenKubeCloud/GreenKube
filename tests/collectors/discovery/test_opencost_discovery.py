# tests/collectors/discovery/test_opencost_discovery.py
"""
TDD tests for service discovery helpers.

We will mock the Kubernetes client CoreV1Api and its
list_service_for_all_namespaces() return value to simulate services
existing in different namespaces.
"""

from types import SimpleNamespace

import pytest

from greenkube.collectors import discovery


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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

    url = await discovery.discover_service_dns("opencost")
    assert url is not None
    assert ":9003" in url
