# tests/core/test_collection_orchestrator.py
"""
Tests for CollectionOrchestrator error resilience and data collection.

The orchestrator must:
- Execute all collectors in parallel via asyncio.gather
- Continue gracefully when one or more collectors fail (no crash, empty defaults)
- Enrich Prometheus instance types from nodes_info when Prometheus labels are absent
- Build the pod_request_map_simple and pod_request_map_agg from pod metrics

These tests are critical for production stability: a single collector failure
(e.g., Prometheus timeout) must never prevent other metrics from being collected.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.collection_orchestrator import CollectionOrchestrator
from greenkube.models.metrics import CostMetric, PodMetric
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import NodeInstanceType, PodCPUUsage, PrometheusMetric

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prometheus_metric(with_instance_types: bool = True) -> PrometheusMetric:
    """Build a minimal PrometheusMetric for testing."""
    cpu_items = [PodCPUUsage(namespace="ns-1", pod="pod-a", container="app", node="node-1", cpu_usage_cores=0.5)]
    instance_types = [NodeInstanceType(node="node-1", instance_type="m5.large")] if with_instance_types else []
    return PrometheusMetric(pod_cpu_usage=cpu_items, node_instance_types=instance_types)


def _make_cost_metrics():
    return [CostMetric(pod_name="pod-a", namespace="ns-1", cpu_cost=0.1, ram_cost=0.2, total_cost=0.3)]


def _make_pod_metrics():
    return [
        PodMetric(
            pod_name="pod-a",
            namespace="ns-1",
            container_name="app",
            cpu_request=500,
            memory_request=256 * 1024 * 1024,
        )
    ]


@pytest.fixture
def mock_prometheus():
    mock = MagicMock()
    mock.collect = AsyncMock(return_value=_make_prometheus_metric())
    return mock


@pytest.fixture
def mock_opencost():
    mock = MagicMock()
    mock.collect = AsyncMock(return_value=_make_cost_metrics())
    return mock


@pytest.fixture
def mock_pod_collector():
    mock = MagicMock()
    mock.collect = AsyncMock(return_value=_make_pod_metrics())
    return mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestCollectionOrchestratorHappyPath:
    """Tests for correct data collection when all collectors succeed."""

    @pytest.mark.asyncio
    async def test_collect_all_returns_prom_metrics(self, mock_prometheus, mock_opencost, mock_pod_collector):
        """prom_metrics is populated from the Prometheus collector."""
        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert result.prom_metrics is not None

    @pytest.mark.asyncio
    async def test_collect_all_returns_cost_map(self, mock_prometheus, mock_opencost, mock_pod_collector):
        """cost_map is keyed by pod_name from OpenCost."""
        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert "pod-a" in result.cost_map
        assert result.cost_map["pod-a"].total_cost == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_collect_all_returns_pod_metrics(self, mock_prometheus, mock_opencost, mock_pod_collector):
        """pod_metrics_list contains the pods collected from the PodCollector."""
        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert len(result.pod_metrics_list) == 1
        assert result.pod_metrics_list[0].pod_name == "pod-a"

    @pytest.mark.asyncio
    async def test_pod_request_map_simple_built_from_pod_metrics(
        self, mock_prometheus, mock_opencost, mock_pod_collector
    ):
        """pod_request_map_simple maps (namespace, pod_name) → CPU request in cores."""
        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        key = ("ns-1", "pod-a")
        assert key in result.pod_request_map_simple
        # 500 millicores → 0.5 cores
        assert result.pod_request_map_simple[key] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_node_instance_types_extracted_from_prometheus(
        self, mock_prometheus, mock_opencost, mock_pod_collector
    ):
        """Instance types present in Prometheus labels populate node_instance_map."""
        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert "node-1" in result.node_instance_map
        assert result.node_instance_map["node-1"] == "m5.large"

    @pytest.mark.asyncio
    async def test_instance_types_enriched_from_nodes_info_when_prometheus_has_none(
        self, mock_opencost, mock_pod_collector
    ):
        """When Prometheus has no instance types, they are enriched from nodes_info passed in."""
        mock_prom = MagicMock()
        mock_prom.collect = AsyncMock(return_value=_make_prometheus_metric(with_instance_types=False))

        nodes_info = {
            "node-1": NodeInfo(
                name="node-1",
                zone="us-east-1a",
                region="us-east-1",
                cloud_provider="aws",
                instance_type="m5.2xlarge",
                architecture="amd64",
                node_pool=None,
            )
        }
        orchestrator = CollectionOrchestrator(mock_prom, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all(nodes_info=nodes_info)

        assert "node-1" in result.node_instance_map
        assert result.node_instance_map["node-1"] == "m5.2xlarge"

    @pytest.mark.asyncio
    async def test_node_without_instance_type_not_in_map(self, mock_opencost, mock_pod_collector):
        """A node without an instance_type in nodes_info is not added to node_instance_map."""
        mock_prom = MagicMock()
        mock_prom.collect = AsyncMock(return_value=_make_prometheus_metric(with_instance_types=False))

        nodes_info = {
            "node-1": NodeInfo(
                name="node-1",
                zone="us-east-1a",
                region="us-east-1",
                cloud_provider="aws",
                instance_type=None,  # No instance type
                architecture="amd64",
                node_pool=None,
            )
        }
        orchestrator = CollectionOrchestrator(mock_prom, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all(nodes_info=nodes_info)

        assert "node-1" not in result.node_instance_map


# ---------------------------------------------------------------------------
# Error resilience — each collector can fail independently
# ---------------------------------------------------------------------------


class TestCollectionOrchestratorResilience:
    """Tests that a failing collector never crashes the whole collection pipeline."""

    @pytest.mark.asyncio
    async def test_prometheus_failure_returns_none_prom_metrics(self, mock_opencost, mock_pod_collector):
        """When Prometheus raises, prom_metrics is None but OpenCost and Pod still collected."""
        mock_prom = MagicMock()
        mock_prom.collect = AsyncMock(side_effect=Exception("Prometheus connection refused"))

        orchestrator = CollectionOrchestrator(mock_prom, mock_opencost, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert result.prom_metrics is None
        assert result.node_instance_map == {}
        # Other collectors must still succeed
        assert "pod-a" in result.cost_map
        assert len(result.pod_metrics_list) == 1

    @pytest.mark.asyncio
    async def test_opencost_failure_returns_empty_cost_map(self, mock_prometheus, mock_pod_collector):
        """When OpenCost raises, cost_map is empty but Prometheus and Pod still collected."""
        mock_oc = MagicMock()
        mock_oc.collect = AsyncMock(side_effect=RuntimeError("OpenCost timeout"))

        orchestrator = CollectionOrchestrator(mock_prometheus, mock_oc, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert result.cost_map == {}
        assert result.prom_metrics is not None
        assert len(result.pod_metrics_list) == 1

    @pytest.mark.asyncio
    async def test_pod_collector_failure_returns_empty_pod_data(self, mock_prometheus, mock_opencost):
        """When PodCollector raises, pod data is empty but Prometheus and OpenCost still collected."""
        mock_pod = MagicMock()
        mock_pod.collect = AsyncMock(side_effect=ConnectionError("K8s API unavailable"))

        orchestrator = CollectionOrchestrator(mock_prometheus, mock_opencost, mock_pod)
        result = await orchestrator.collect_all()

        assert result.pod_metrics_list == []
        assert result.pod_request_map_simple == {}
        assert result.pod_request_map_agg == {}
        # Prometheus and OpenCost should succeed
        assert result.prom_metrics is not None
        assert "pod-a" in result.cost_map

    @pytest.mark.asyncio
    async def test_all_collectors_fail_no_exception_raised(self):
        """When all three collectors fail, no exception is propagated to the caller."""
        mock_prom = MagicMock()
        mock_prom.collect = AsyncMock(side_effect=Exception("Prometheus down"))
        mock_oc = MagicMock()
        mock_oc.collect = AsyncMock(side_effect=Exception("OpenCost down"))
        mock_pod = MagicMock()
        mock_pod.collect = AsyncMock(side_effect=Exception("K8s down"))

        orchestrator = CollectionOrchestrator(mock_prom, mock_oc, mock_pod)

        # Must NOT raise — production must never crash on collection errors
        result = await orchestrator.collect_all()

        assert result.prom_metrics is None
        assert result.cost_map == {}
        assert result.pod_metrics_list == []
        assert result.node_instance_map == {}

    @pytest.mark.asyncio
    async def test_empty_opencost_list_produces_empty_cost_map(self, mock_prometheus, mock_pod_collector):
        """When OpenCost returns an empty list, cost_map is empty but no error is raised."""
        mock_oc = MagicMock()
        mock_oc.collect = AsyncMock(return_value=[])

        orchestrator = CollectionOrchestrator(mock_prometheus, mock_oc, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert result.cost_map == {}

    @pytest.mark.asyncio
    async def test_cost_metric_without_pod_name_excluded_from_map(self, mock_prometheus, mock_pod_collector):
        """CostMetric entries with an empty pod_name are excluded from cost_map."""
        mock_oc = MagicMock()
        mock_oc.collect = AsyncMock(
            return_value=[
                CostMetric(pod_name="", namespace="ns", cpu_cost=0.0, ram_cost=0.0, total_cost=0.1),
                CostMetric(pod_name="pod-x", namespace="ns", cpu_cost=0.1, ram_cost=0.1, total_cost=0.2),
            ]
        )

        orchestrator = CollectionOrchestrator(mock_prometheus, mock_oc, mock_pod_collector)
        result = await orchestrator.collect_all()

        assert "" not in result.cost_map
        assert "pod-x" in result.cost_map
