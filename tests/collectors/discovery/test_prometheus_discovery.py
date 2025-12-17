# tests/collectors/discovery/test_prometheus_discovery.py
"""
TDD tests for service discovery helpers.

We will mock the Kubernetes client CoreV1Api and its
list_service_for_all_namespaces() return value to simulate services
existing in different namespaces.
"""

from types import SimpleNamespace

import pytest


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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

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

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda: MockCore())

    url = await discovery.discover_service_dns("prometheus")
    assert url is not None
    # should pick the monitoring one with labels
    assert "prometheus-k8s.monitoring.svc.cluster.local" in url
    assert ":9090" in url
