from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.discovery.base import BaseDiscovery


def _service(name, namespace, ports, labels=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, labels=labels or {}),
        spec=SimpleNamespace(ports=[SimpleNamespace(port=port.get("port"), name=port.get("name")) for port in ports]),
    )


def test_pick_port_handles_empty_and_fallback_ports():
    discovery = BaseDiscovery()

    assert discovery.pick_port([]) is None
    assert discovery.pick_port([SimpleNamespace(port=1234, name="metrics")]) == 1234


def test_build_dns_and_parts():
    discovery = BaseDiscovery()

    assert discovery.build_dns("api", "monitoring", 9090) == "http://api.monitoring.svc.cluster.local:9090"
    assert discovery.build_parts("api", "monitoring", 443, scheme="https") == (
        "https",
        "api.monitoring.svc.cluster.local",
        443,
    )


def test_is_resolvable_respects_skip_env(monkeypatch):
    monkeypatch.setenv("GREENKUBE_DISCOVERY_SKIP_DNS_CHECK", "1")

    assert BaseDiscovery()._is_resolvable("not-a-real-service.invalid") is True


def test_is_resolvable_returns_false_for_dns_failure(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GREENKUBE_DISCOVERY_SKIP_DNS_CHECK", raising=False)
    monkeypatch.setattr("greenkube.collectors.discovery.base.socket.getaddrinfo", MagicMock(side_effect=OSError))

    assert BaseDiscovery()._is_resolvable("not-a-real-service.invalid") is False


@pytest.mark.asyncio
async def test_list_services_returns_none_when_config_unavailable(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", AsyncMock(return_value=False))

    assert await BaseDiscovery().list_services() is None


@pytest.mark.asyncio
async def test_list_services_returns_items_with_mocked_kubernetes_client(monkeypatch):
    services = [object()]

    class MockApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class MockCoreV1Api:
        async def list_service_for_all_namespaces(self):
            return SimpleNamespace(items=services)

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("greenkube.collectors.discovery.base.ensure_k8s_config", AsyncMock(return_value=True))
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.ApiClient", MockApiClient)
    monkeypatch.setattr("greenkube.collectors.discovery.base.client.CoreV1Api", lambda api_client: MockCoreV1Api())

    assert await BaseDiscovery().list_services() == services


@pytest.mark.asyncio
async def test_discover_selects_highest_scored_resolvable_candidate(monkeypatch):
    discovery = BaseDiscovery()
    discovery._collect_candidates = AsyncMock(
        return_value=[
            (10, "prometheus-low", "default", 9090, "http"),
            (50, "prometheus-high", "monitoring", 443, "https"),
        ]
    )
    monkeypatch.setattr(discovery, "_is_running_in_cluster", lambda: False)
    monkeypatch.setattr(discovery, "_is_resolvable", lambda host: True)

    assert await discovery.discover("prometheus") == "https://prometheus-high.monitoring.svc.cluster.local:443"


@pytest.mark.asyncio
async def test_discover_returns_none_when_best_candidate_is_not_reachable(monkeypatch):
    discovery = BaseDiscovery()
    discovery._collect_candidates = AsyncMock(return_value=[(10, "svc", "default", 80, "http")])
    monkeypatch.setattr(discovery, "_is_running_in_cluster", lambda: False)
    monkeypatch.setattr(discovery, "_is_resolvable", lambda host: False)

    assert await discovery.discover("svc") is None


@pytest.mark.asyncio
async def test_probe_candidates_skips_unresolvable_and_returns_first_success(monkeypatch):
    discovery = BaseDiscovery()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(discovery, "_is_running_in_cluster", lambda: False)
    monkeypatch.setattr(discovery, "_is_resolvable", lambda host: "good" in host)

    probe = AsyncMock(side_effect=[True])
    result = await discovery.probe_candidates(
        [
            (50, "bad", "monitoring", 9090, "http"),
            (40, "good", "monitoring", 9090, "http"),
        ],
        probe,
    )

    assert result == "http://good.monitoring.svc.cluster.local:9090"
    probe.assert_awaited_once_with("http://good.monitoring.svc.cluster.local:9090", 40)


@pytest.mark.asyncio
async def test_probe_candidates_returns_none_when_probe_fails(monkeypatch):
    discovery = BaseDiscovery()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(discovery, "_is_running_in_cluster", lambda: True)

    assert await discovery.probe_candidates([(10, "svc", "default", 80, "http")], AsyncMock(return_value=False)) is None


@pytest.mark.asyncio
async def test_collect_candidates_scores_labels_ports_namespaces_and_tls(monkeypatch):
    discovery = BaseDiscovery()
    discovery.list_services = AsyncMock(
        return_value=[
            _service("prometheus-adapter", "monitoring", [{"port": 443, "name": "https"}]),
            _service(
                "prometheus-secure",
                "default",
                [{"port": 443, "name": "https"}],
                labels={"app.kubernetes.io/name": "prometheus"},
            ),
            _service(
                "prometheus-k8s",
                "monitoring",
                [{"port": 9090, "name": "web"}, {"port": 8080, "name": "http"}],
                labels={"app.kubernetes.io/name": "prometheus"},
            ),
            _service("ignored", "default", []),
            _service("unrelated", "default", [{"port": 1234, "name": "metrics"}]),
        ]
    )

    candidates = await discovery._collect_candidates(
        "prometheus",
        prefer_namespaces=("monitoring",),
        prefer_ports=(9090,),
        prefer_labels={"app.kubernetes.io/name": "prometheus"},
    )

    assert (41, "prometheus-k8s", "monitoring", 9090, "http") in candidates
    assert any(candidate[1] == "prometheus-secure" and candidate[4] == "https" for candidate in candidates)
    assert all(candidate[1] != "prometheus-adapter" for candidate in candidates)
    assert all(candidate[1] != "unrelated" for candidate in candidates)
