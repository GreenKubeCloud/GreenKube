# tests/collectors/test_discovery.py
"""
TDD tests for service discovery helpers.

We will mock the Kubernetes client CoreV1Api and its
list_service_for_all_namespaces() return value to simulate services
existing in different namespaces.
"""

from types import SimpleNamespace

from greenkube.collectors.discovery.base import BaseDiscovery


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


def test_pick_port_prefers_named_http():
    bd = BaseDiscovery()
    ports = [SimpleNamespace(port=1234, name="metrics"), SimpleNamespace(port=8080, name="http")]
    assert bd.pick_port(ports) == 8080
