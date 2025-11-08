from kubernetes import client, config

from .base import BaseDiscovery
from .opencost import OpenCostDiscovery
from .prometheus import PrometheusDiscovery

# Expose kubernetes client/config at package level so tests can monkeypatch
# paths like 'greenkube.collectors.discovery.client.CoreV1Api'.
__all__ = ["BaseDiscovery", "PrometheusDiscovery", "OpenCostDiscovery", "discover_service_dns", "client", "config"]


def discover_service_dns(hint: str):
    """Compatibility helper to discover a service by hint.

    Delegates to specialized discovery classes for known hints.
    """
    try:
        if not hint:
            return None
        lh = hint.lower()
        if lh == "prometheus":
            return PrometheusDiscovery().discover()
        if lh == "opencost":
            return OpenCostDiscovery().discover()
        # Fallback to base discovery for other hints
        return BaseDiscovery().discover(lh)
    except Exception:
        return None
