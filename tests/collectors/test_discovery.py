# tests/collectors/test_discovery.py
"""
TDD tests for service discovery helpers.

We will mock the Kubernetes client CoreV1Api and its
list_service_for_all_namespaces() return value to simulate services
existing in different namespaces.
"""

from types import SimpleNamespace


def make_svc(name, namespace, ports):
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
    return svc


def test_discover_prometheus_by_name(monkeypatch):
    from greenkube.collectors import discovery

    # Mock CoreV1Api to return a list with a prometheus service
    svc1 = make_svc("prometheus-k8s", "monitoring", [{"port": 9090, "name": "http"}])
    svc2 = make_svc("some-other", "default", [{"port": 8080, "name": "http"}])

    class MockCore:
        def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc1, svc2])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())

    url = discovery.discover_service_dns("prometheus")
    assert url is not None
    assert "prometheus-k8s.monitoring.svc.cluster.local" in url
    assert ":9090" in url


def test_discover_prometheus_not_found(monkeypatch):
    from greenkube.collectors import discovery

    svc = make_svc("other", "default", [{"port": 8080, "name": "http"}])

    class MockCore:
        def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())

    url = discovery.discover_service_dns("prometheus")
    assert url is None


def test_discover_opencost_by_name_and_port(monkeypatch):
    from greenkube.collectors import discovery

    svc = make_svc("opencost-api", "opencost", [{"port": 8080, "name": "http"}])

    class MockCore:
        def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())

    url = discovery.discover_service_dns("opencost")
    assert url is not None
    assert "opencost-api.opencost.svc.cluster.local" in url
    assert ":8080" in url


def test_discover_opencost_not_found(monkeypatch):
    from greenkube.collectors import discovery

    svc = make_svc("some", "default", [{"port": 9090, "name": "http"}])

    class MockCore:
        def list_service_for_all_namespaces(self, **kwargs):
            return SimpleNamespace(items=[svc])

    monkeypatch.setattr("greenkube.collectors.discovery.client.CoreV1Api", lambda: MockCore())

    url = discovery.discover_service_dns("opencost")
    assert url is None
