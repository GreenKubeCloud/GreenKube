# tests/collectors/test_lb_collector.py
"""
Tests for the LoadBalancerCollector that detects orphaned LoadBalancer Services.
TDD: Tests written before implementation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.lb_collector import LoadBalancerCollector, OrphanedLoadBalancer, enrich_orphaned_lb_costs


@pytest.fixture
def lb_collector():
    """Returns a LoadBalancerCollector instance."""
    return LoadBalancerCollector()


def _make_service(name, namespace, svc_type="LoadBalancer", ports=None, ingress=None, selector=None):
    """Helper to create a mock Service object."""
    svc = MagicMock()
    svc.metadata = MagicMock()
    svc.metadata.name = name
    svc.metadata.namespace = namespace
    svc.spec = MagicMock()
    svc.spec.type = svc_type
    svc.spec.ports = ports or [MagicMock()]
    svc.spec.selector = selector
    svc.status = MagicMock()
    svc.status.load_balancer = MagicMock()
    svc.status.load_balancer.ingress = ingress or []
    return svc


def _make_port(port, protocol="TCP"):
    """Helper to create a mock service port."""
    p = MagicMock()
    p.port = port
    p.protocol = protocol
    return p


def _make_endpoints(namespace, name, address_count=0):
    """Helper to create a mock Endpoints object."""
    ep = MagicMock()
    ep.metadata = MagicMock()
    ep.metadata.namespace = namespace
    ep.metadata.name = name
    ep.subsets = []
    if address_count:
        subset = MagicMock()
        subset.addresses = [MagicMock() for _ in range(address_count)]
        ep.subsets = [subset]
    return ep


def _make_ingress(ip=None, hostname=None):
    """Helper to create a mock load balancer ingress entry."""
    ingress = MagicMock()
    ingress.ip = ip
    ingress.hostname = hostname
    return ingress


def _mock_api(services, endpoints):
    """Creates a mock CoreV1Api returning the given Service and Endpoints lists."""
    api = AsyncMock()
    service_list = MagicMock()
    service_list.items = services
    endpoints_list = MagicMock()
    endpoints_list.items = endpoints
    api.list_service_for_all_namespaces = AsyncMock(return_value=service_list)
    api.list_endpoints_for_all_namespaces = AsyncMock(return_value=endpoints_list)
    return api


class TestLoadBalancerCollector:
    """Tests for LoadBalancerCollector.collect()."""

    @pytest.mark.asyncio
    async def test_collect_detects_lb_without_endpoints(self, lb_collector):
        """A LoadBalancer Service with no endpoints should be flagged."""
        svc = _make_service(
            "dead-lb",
            "legacy",
            ports=[_make_port(443)],
            ingress=[_make_ingress(ip="1.2.3.4")],
        )
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([svc], []),
        ):
            orphaned = await lb_collector.collect()

        assert len(orphaned) == 1
        result = orphaned[0]
        assert result.name == "dead-lb"
        assert result.namespace == "legacy"
        assert result.endpoint_count == 0
        assert result.external_ip == "1.2.3.4"
        assert result.ports == "443/TCP"

    @pytest.mark.asyncio
    async def test_collect_skips_lb_with_live_endpoints(self, lb_collector):
        """A LoadBalancer Service with ready endpoints should NOT be flagged."""
        svc = _make_service("live-lb", "default", ports=[_make_port(80)])
        ep = _make_endpoints("default", "live-lb", address_count=2)
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([svc], [ep]),
        ):
            orphaned = await lb_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_skips_non_load_balancer_services(self, lb_collector):
        """ClusterIP and NodePort services should never be flagged."""
        cluster_ip = _make_service("web", "default", svc_type="ClusterIP")
        node_port = _make_service("web-nodeport", "default", svc_type="NodePort")
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([cluster_ip, node_port], []),
        ):
            orphaned = await lb_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_handles_missing_endpoints_object(self, lb_collector):
        """A LoadBalancer with no Endpoints object at all should be flagged."""
        svc = _make_service("ghost-lb", "prod", ports=[_make_port(8080)])
        other_ep = _make_endpoints("prod", "other-service", address_count=1)
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([svc], [other_ep]),
        ):
            orphaned = await lb_collector.collect()

        assert len(orphaned) == 1
        assert orphaned[0].name == "ghost-lb"
        assert orphaned[0].namespace == "prod"

    @pytest.mark.asyncio
    async def test_collect_uses_hostname_when_ip_missing(self, lb_collector):
        """The external hostname should be captured when no IP is assigned."""
        svc = _make_service(
            "dns-lb",
            "default",
            ports=[_make_port(53, protocol="UDP")],
            ingress=[_make_ingress(hostname="abc123.elb.eu-west-1.amazonaws.com")],
        )
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=_mock_api([svc], []),
        ):
            orphaned = await lb_collector.collect()

        assert len(orphaned) == 1
        assert orphaned[0].external_ip == "abc123.elb.eu-west-1.amazonaws.com"
        assert orphaned[0].ports == "53/UDP"

    @pytest.mark.asyncio
    async def test_collect_returns_empty_when_api_unavailable(self, lb_collector):
        """Should return an empty list if the K8s API is unavailable."""
        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            orphaned = await lb_collector.collect()

        assert orphaned == []

    @pytest.mark.asyncio
    async def test_collect_handles_exception_gracefully(self, lb_collector):
        """Should return an empty list on exception without crashing."""
        mock_api = AsyncMock()
        mock_api.list_service_for_all_namespaces = AsyncMock(side_effect=Exception("API timeout"))

        with patch(
            "greenkube.collectors.lb_collector.get_core_v1_api",
            new_callable=AsyncMock,
            return_value=mock_api,
        ):
            orphaned = await lb_collector.collect()

        assert orphaned == []


# ---------------------------------------------------------------------------
# enrich_orphaned_lb_costs
# ---------------------------------------------------------------------------


class TestEnrichOrphanedLbCosts:
    """Tests for the OpenCost cost enrichment of orphaned LoadBalancers."""

    @pytest.mark.asyncio
    async def test_annualizes_opencost_window_costs(self):
        """Window costs should be annualized against the observation window."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        services = [
            OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0),
            OrphanedLoadBalancer(name="lb-bbb", namespace="default", endpoint_count=0),
        ]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_costs = AsyncMock(return_value={"default/lb-aaa": 2.5})

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        # 2.5 USD over 7 days → 2.5 * 365/7 per year
        assert result[0].annual_cost == pytest.approx(2.5 * 365 / 7)
        assert result[1].annual_cost is None
        mock_opencost.collect_lb_costs.assert_awaited_once_with(["default/lb-aaa", "default/lb-bbb"], window_days=7)

    @pytest.mark.asyncio
    async def test_keeps_services_unchanged_without_opencost_data(self):
        """Services without OpenCost data should keep annual_cost=None."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        services = [OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_costs = AsyncMock(return_value={})

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_handles_opencost_failure_gracefully(self):
        """An OpenCost failure should leave services untouched without raising."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        services = [OrphanedLoadBalancer(name="lb-aaa", namespace="default", endpoint_count=0)]
        mock_opencost = AsyncMock(spec=OpenCostCollector)
        mock_opencost.collect_lb_costs = AsyncMock(side_effect=RuntimeError("boom"))

        result = await enrich_orphaned_lb_costs(services, window_days=7, opencost=mock_opencost)

        assert result is services
        assert result[0].annual_cost is None

    @pytest.mark.asyncio
    async def test_skips_opencost_for_empty_services(self):
        """No services means no OpenCost query."""
        from greenkube.collectors.opencost_collector import OpenCostCollector

        mock_opencost = AsyncMock(spec=OpenCostCollector)

        result = await enrich_orphaned_lb_costs([], opencost=mock_opencost)

        assert result == []
        mock_opencost.collect_lb_costs.assert_not_awaited()
