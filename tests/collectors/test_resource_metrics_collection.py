# tests/collectors/test_resource_metrics_collection.py
"""
Tests for extended resource metrics collection from Prometheus.

Validates that the PrometheusCollector correctly fetches and parses:
- Network I/O (bytes received/transmitted)
- Disk/Storage I/O (bytes read/written)
- Pod restart counts
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.core.config import Config
from greenkube.models.prometheus_metrics import (
    PrometheusMetric,
)


@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=Config)
    settings.PROMETHEUS_URL = "http://prometheus:9090"
    settings.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    settings.PROMETHEUS_VERIFY_CERTS = True
    settings.PROMETHEUS_BEARER_TOKEN = None
    settings.PROMETHEUS_USERNAME = None
    settings.PROMETHEUS_PASSWORD = None
    settings.PROMETHEUS_NODE_INSTANCE_LABEL = "label_node_kubernetes_io_instance_type"
    return settings


class TestNetworkIOCollection:
    """Tests for network I/O metric collection."""

    @pytest.mark.asyncio
    async def test_parse_network_receive_data(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "web-app-1",
                "node": "node-1",
            },
            "value": [1700000000, "1048576"],
        }
        result = collector._parse_network_receive_data(item)
        assert result is not None
        assert result.namespace == "default"
        assert result.pod == "web-app-1"
        assert result.node == "node-1"
        assert result.network_receive_bytes == 1048576.0

    @pytest.mark.asyncio
    async def test_parse_network_transmit_data(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "web-app-1",
                "node": "node-1",
            },
            "value": [1700000000, "524288"],
        }
        result = collector._parse_network_transmit_data(item)
        assert result is not None
        assert result.namespace == "default"
        assert result.pod == "web-app-1"
        assert result.node == "node-1"
        assert result.network_transmit_bytes == 524288.0

    @pytest.mark.asyncio
    async def test_parse_network_data_missing_fields(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {"namespace": "default"},
            "value": [1700000000, "1048576"],
        }
        result = collector._parse_network_receive_data(item)
        assert result is None

    @pytest.mark.asyncio
    async def test_parse_network_data_nan_value(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "web-app-1",
                "node": "node-1",
            },
            "value": [1700000000, "NaN"],
        }
        result = collector._parse_network_receive_data(item)
        assert result is None


class TestDiskIOCollection:
    """Tests for disk I/O metric collection."""

    @pytest.mark.asyncio
    async def test_parse_disk_read_data(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "db-pod-1",
                "node": "node-1",
                "device": "sda",
            },
            "value": [1700000000, "2097152"],
        }
        result = collector._parse_disk_read_data(item)
        assert result is not None
        assert result.namespace == "default"
        assert result.pod == "db-pod-1"
        assert result.node == "node-1"
        assert result.disk_read_bytes == 2097152.0

    @pytest.mark.asyncio
    async def test_parse_disk_write_data(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "db-pod-1",
                "node": "node-1",
                "device": "sda",
            },
            "value": [1700000000, "4194304"],
        }
        result = collector._parse_disk_write_data(item)
        assert result is not None
        assert result.disk_write_bytes == 4194304.0

    @pytest.mark.asyncio
    async def test_parse_disk_data_missing_fields(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {"namespace": "default"},
            "value": [1700000000, "2097152"],
        }
        result = collector._parse_disk_read_data(item)
        assert result is None


class TestRestartCountCollection:
    """Tests for pod restart count collection."""

    @pytest.mark.asyncio
    async def test_parse_restart_count_data(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "crash-pod-1",
                "container": "app",
                "node": "node-1",
            },
            "value": [1700000000, "5"],
        }
        result = collector._parse_restart_count_data(item)
        assert result is not None
        assert result.namespace == "default"
        assert result.pod == "crash-pod-1"
        assert result.restart_count == 5

    @pytest.mark.asyncio
    async def test_parse_restart_count_zero(self, mock_settings):
        collector = PrometheusCollector(mock_settings)
        item = {
            "metric": {
                "namespace": "default",
                "pod": "stable-pod",
                "container": "app",
                "node": "node-1",
            },
            "value": [1700000000, "0"],
        }
        result = collector._parse_restart_count_data(item)
        assert result is not None
        assert result.restart_count == 0


class TestPrometheusCollectIntegration:
    """Tests that collect() returns all extended metrics."""

    @pytest.mark.asyncio
    async def test_collect_returns_all_metric_types(self, mock_settings):
        collector = PrometheusCollector(mock_settings)

        # Raw Prometheus result rows that _query_prometheus would return
        cpu_results = [
            {"metric": {"namespace": "ns", "pod": "p1", "container": "c1", "node": "n1"}, "value": [1700000000, "0.5"]}
        ]
        mem_results = [{"metric": {"namespace": "ns", "pod": "p1", "node": "n1"}, "value": [1700000000, "104857600"]}]
        node_results = []
        net_rx_results = [{"metric": {"namespace": "ns", "pod": "p1", "node": "n1"}, "value": [1700000000, "1024000"]}]
        net_tx_results = [{"metric": {"namespace": "ns", "pod": "p1", "node": "n1"}, "value": [1700000000, "512000"]}]
        disk_read_results = [
            {"metric": {"namespace": "ns", "pod": "p1", "node": "n1"}, "value": [1700000000, "2048000"]}
        ]
        disk_write_results = [
            {"metric": {"namespace": "ns", "pod": "p1", "node": "n1"}, "value": [1700000000, "1024000"]}
        ]
        restart_results = [
            {"metric": {"namespace": "ns", "pod": "p1", "container": "c1", "node": "n1"}, "value": [1700000000, "2"]}
        ]

        # Map queries to their results
        query_map = {
            collector.cpu_usage_query: cpu_results,
            collector.node_labels_query: node_results,
            collector.memory_usage_query: mem_results,
            collector.network_receive_query: net_rx_results,
            collector.network_transmit_query: net_tx_results,
            collector.disk_read_query: disk_read_results,
            collector.disk_write_query: disk_write_results,
            collector.restart_count_query: restart_results,
        }

        async def mock_query(client, query):
            return query_map.get(query, [])

        with patch.object(collector, "_query_prometheus", side_effect=mock_query):
            with patch.object(collector, "_get_client", return_value=AsyncMock()):
                result = await collector.collect()

        assert isinstance(result, PrometheusMetric)
        assert len(result.pod_cpu_usage) >= 1
        assert len(result.pod_memory_usage) >= 1
        assert len(result.pod_network_io) >= 1
        assert len(result.pod_disk_io) >= 1
        assert len(result.pod_restart_counts) >= 1
