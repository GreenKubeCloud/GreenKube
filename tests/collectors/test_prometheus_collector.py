# tests/collectors/test_prometheus_collector.py
"""
Tests for the PrometheusCollector using Test-Driven Development (TDD).

We will mock all HTTP requests to the Prometheus API using respx.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from greenkube.collectors.discovery.prometheus import PrometheusDiscovery
from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.core.config import Config
from greenkube.models.prometheus_metrics import (
    PodCPUUsage,
    PrometheusMetric,
)

# --- Mock Prometheus API Responses ---

MOCK_CPU_USAGE_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {
                    "container": "app",
                    "namespace": "prod",
                    "pod": "api-deployment-12345",
                    "node": "node-1",
                },
                "value": [1678886400, "0.5"],  # 0.5 cores
            },
            {
                "metric": {
                    "container": "db",
                    "namespace": "prod",
                    "pod": "db-deployment-67890",
                    "node": "node-2",
                },
                "value": [1678886400, "1.2"],  # 1.2 cores
            },
            {
                "metric": {
                    # Missing 'pod' label, should be skipped
                    "container": "sidecar",
                    "namespace": "default",
                    "node": "node-1",
                },
                "value": [1678886400, "0.1"],
            },
            {
                "metric": {
                    # Missing 'node' label, should be skipped
                    "container": "missing-node-container",
                    "namespace": "prod",
                    "pod": "api-deployment-77777",
                },
                "value": [1678886400, "0.2"],
            },
            {
                "metric": {
                    "container": "nan-value-container",
                    "namespace": "prod",
                    "pod": "api-deployment-99999",
                    "node": "node-2",
                },
                "value": [1678886400, "NaN"],  # Should be skipped
            },
        ],
    },
}

MOCK_NODE_LABELS_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {
                    "node": "node-1",
                    "label_node_kubernetes_io_instance_type": "m5.large",
                },
                "value": [1678886400, "1"],
            },
            {
                "metric": {
                    "node": "node-2",
                    "label_node_kubernetes_io_instance_type": "t3.medium",
                },
                "value": [1678886400, "1"],
            },
            {
                "metric": {
                    # Missing instance type label, should be skipped
                    "node": "node-3"
                },
                "value": [1678886400, "1"],
            },
        ],
    },
}

MOCK_EMPTY_RESPONSE = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}

MOCK_CPU_RANGE_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"namespace": "prod", "pod": "api-1", "node": "node-1"},
                "values": [[1678886400, "0.5"], [1678886700, "0.6"]],
            }
        ],
    },
}


# --- Pytest Fixtures ---


@pytest.fixture
def mock_config():
    """Fixture to create a Config instance and patch its attributes for tests."""

    config = Config()
    config.PROMETHEUS_URL = "http://mock-prometheus:9090"
    config.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    config.PROMETHEUS_VERIFY_CERTS = True
    return config


@pytest.fixture
def collector(mock_config):
    """Return a PrometheusCollector instance with a mocked config."""
    return PrometheusCollector(settings=mock_config)


# --- Test Cases ---


@pytest.mark.asyncio
@respx.mock
async def test_collect_success(collector):
    """
    Test the happy path: Prometheus is reachable and returns valid data.
    """
    mock_url = collector.base_url

    # Mock CPU query
    query_params_cpu = {"query": collector.cpu_usage_query}
    url_cpu_pattern = respx.get(f"{mock_url}/api/v1/query", params=query_params_cpu)
    url_cpu_pattern.mock(return_value=Response(200, json=MOCK_CPU_USAGE_RESPONSE))

    # Mock Node query
    query_params_node = {"query": collector.node_labels_query}
    url_node_pattern = respx.get(f"{mock_url}/api/v1/query", params=query_params_node)
    url_node_pattern.mock(return_value=Response(200, json=MOCK_NODE_LABELS_RESPONSE))

    # Use a pattern that matches regardless of param order if above specific params fail
    # But respx handles params matching well.

    result = await collector.collect()

    assert isinstance(result, PrometheusMetric)

    # Check CPU data (2 valid, 3 invalid entries in mock)
    assert len(result.pod_cpu_usage) == 2
    assert result.pod_cpu_usage[0].namespace == "prod"
    assert result.pod_cpu_usage[0].pod == "api-deployment-12345"
    assert result.pod_cpu_usage[0].container == "app"
    assert result.pod_cpu_usage[0].node == "node-1"  # Assert node
    assert result.pod_cpu_usage[0].cpu_usage_cores == 0.5

    assert result.pod_cpu_usage[1].pod == "db-deployment-67890"
    assert result.pod_cpu_usage[1].node == "node-2"  # Assert node
    assert result.pod_cpu_usage[1].cpu_usage_cores == 1.2

    # Check Node data (2 valid, 1 invalid entry in mock)
    assert len(result.node_instance_types) == 2
    assert result.node_instance_types[0].node == "node-1"
    assert result.node_instance_types[0].instance_type == "m5.large"
    assert result.node_instance_types[1].node == "node-2"
    assert result.node_instance_types[1].instance_type == "t3.medium"


@pytest.mark.asyncio
@respx.mock
@patch.object(PrometheusDiscovery, "discover")
async def test_collect_with_no_url_triggers_discovery(mock_discover, collector):
    """
    Test that collect() triggers discovery when PROMETHEUS_URL is not set.
    """
    # We need a collector with no URL
    collector.base_url = None

    # 1. Discovery is mocked to return a new URL
    discovered_url = "http://discovered-prometheus:9090"
    mock_discover.return_value = discovered_url

    # 2. Mock the API calls to the *discovered* URL.
    # Use generic matching for simplicity as we call query twice
    respx.get(f"{discovered_url}/api/v1/query").mock(
        side_effect=[Response(200, json=MOCK_CPU_USAGE_RESPONSE), Response(200, json=MOCK_NODE_LABELS_RESPONSE)]
    )

    # 3. Call collect - this should now succeed via discovery
    # Note: connect_timeout logic in async client might need attention if we rely on it
    result = await collector.collect()

    # 4. Assertions
    mock_discover.assert_called_once()
    assert collector.base_url == discovered_url
    assert len(result.pod_cpu_usage) == 2
    assert len(result.node_instance_types) == 2


@pytest.mark.asyncio
@respx.mock
async def test_collect_connection_error(collector):
    """
    Test behavior when Prometheus is unreachable.
    """
    respx.get(f"{collector.base_url}/api/v1/query").mock(side_effect=httpx.ConnectError("Connection refused"))

    result = await collector.collect()

    # Should fail gracefully and return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_api_error(collector):
    """
    Test behavior when Prometheus returns an HTTP 500 or other error.
    """
    respx.get(f"{collector.base_url}/api/v1/query").mock(
        return_value=Response(500, json={"status": "error", "error": "Internal error"})
    )

    result = await collector.collect()

    # Should fail gracefully and return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_empty_results(collector):
    """
    Test behavior when Prometheus is reachable but returns no data.
    """
    respx.get(f"{collector.base_url}/api/v1/query").mock(return_value=Response(200, json=MOCK_EMPTY_RESPONSE))

    result = await collector.collect()

    # Should return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


def test_parsing_malformed_cpu_data(collector):
    """
    Test that malformed data is gracefully handled and returns None.
    """
    # This logic is synchronous parsing, so no async needed
    malformed_cpu_data = {
        "metric": {"namespace": "n", "pod": "p", "container": "c", "node": "n1"},
        "value": [0, "not-a-float"],
    }

    assert collector._parse_cpu_data(malformed_cpu_data) is None


def test_parsing_missing_cpu_labels(collector):
    """
    Test that the parser returns None if key labels are missing.
    """
    # Missing 'pod'
    missing_label_data = {
        "metric": {"namespace": "n", "container": "c", "node": "n1"},
        "value": [0, "0.5"],
    }
    assert collector._parse_cpu_data(missing_label_data) is None

    # Missing 'node'
    missing_label_data_2 = {
        "metric": {"namespace": "n", "pod": "p", "container": "c"},
        "value": [0, "0.5"],
    }
    assert collector._parse_cpu_data(missing_label_data_2) is None


def test_parsing_missing_node_labels(collector):
    """
    Test that the parser returns None if key node labels are missing.
    """
    # Missing 'label_node_kubernetes_io_instance_type'
    missing_label_data = {"metric": {"node": "node-1"}, "value": [0, "1"]}
    assert collector._parse_node_data(missing_label_data) is None


def test_parsing_nan_value(collector):
    """
    Test that values of 'NaN' are gracefully skipped.
    """
    nan_data = {
        "metric": {"namespace": "n", "pod": "p", "container": "c", "node": "n1"},
        "value": [0, "NaN"],
    }
    assert collector._parse_cpu_data(nan_data) is None


@pytest.mark.asyncio
@respx.mock
async def test_collect_ignores_non_pod_series(collector):
    """
    Ensure collect() ignores node-level/non-pod series and only returns pod-level entries.
    """
    mock_url = collector.base_url
    # Build CPU response mixing node-level and pod-level series
    mixed_result = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"node": "node-1"},
                    "value": [1678886400, "0.5"],
                },  # node-level
                {
                    "metric": {
                        "namespace": "prod",
                        "pod": "p1",
                        "container": "c1",
                        "node": "node-1",
                    },
                    "value": [1678886400, "0.7"],
                },
            ],
        },
    }

    query_params_cpu = {"query": collector.cpu_usage_query}
    respx.get(f"{mock_url}/api/v1/query", params=query_params_cpu).mock(return_value=Response(200, json=mixed_result))

    # Node labels empty to avoid interfering
    query_params_node = {"query": collector.node_labels_query}
    respx.get(f"{mock_url}/api/v1/query", params=query_params_node).mock(
        return_value=Response(200, json=MOCK_EMPTY_RESPONSE)
    )

    result = await collector.collect()
    # Only one pod-level entry should be returned
    assert len(result.pod_cpu_usage) == 1
    assert result.pod_cpu_usage[0].pod == "p1"


def test_parse_pod_series_variants(collector):
    """
    Ensure parsing helpers accept pod-level series both with and without 'container' label.
    """
    pod_with_container = {
        "metric": {"namespace": "ns", "pod": "p-a", "container": "ctr", "node": "n1"},
        "value": [0, "0.25"],
    }
    pod_without_container = {
        "metric": {"namespace": "ns", "pod": "p-b", "node": "n1"},
        "value": [0, "0.5"],
    }

    p1 = collector._parse_cpu_data(pod_with_container)
    assert isinstance(p1, PodCPUUsage)
    assert p1.container == "ctr"

    p2 = collector._parse_cpu_data_no_container(pod_without_container)
    assert isinstance(p2, PodCPUUsage)
    assert p2.container == ""


@pytest.mark.asyncio
@respx.mock
@patch.object(PrometheusCollector, "_discover_and_update_url")
async def test_collect_range_with_no_url_triggers_discovery(mock_discover_and_update, collector):
    """
    Test that collect_range() triggers discovery when PROMETHEUS_URL is not set.
    """
    # We need a collector with no URL
    collector.base_url = None
    discovered_url = "http://discovered-prometheus:9090"

    async def side_effect(client):
        collector.base_url = discovered_url
        return True

    mock_discover_and_update.side_effect = side_effect

    # Mock the API call to the discovered URL
    respx.get(f"{discovered_url}/api/v1/query_range").mock(return_value=Response(200, json=MOCK_CPU_RANGE_RESPONSE))

    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)

    # collect_range is async now too
    result = await collector.collect_range(start_time, end_time)

    mock_discover_and_update.assert_called_once()
    assert collector.base_url == discovered_url
    assert len(result) == 1
    assert result[0]["metric"]["pod"] == "api-1"


def test_update_url_respects_config_verify_certs(mock_config):
    """
    Test that _update_url respects the Config default for PROMETHEUS_VERIFY_CERTS.
    """
    # Create a collector with the default config (PROMETHEUS_VERIFY_CERTS = True)
    mock_config.PROMETHEUS_VERIFY_CERTS = True
    collector = PrometheusCollector(settings=mock_config)

    # Initially verify should be True (from config)
    assert collector.verify is True

    # Update URL to HTTPS - verify should remain True (from config)
    collector._update_url("https://discovered-prometheus:9090")
    assert collector.verify is True
    assert collector.base_url == "https://discovered-prometheus:9090"

    # Update URL to HTTP - verify should be False (not applicable for HTTP)
    collector._update_url("http://discovered-prometheus:9090")
    assert collector.verify is False
    assert collector.base_url == "http://discovered-prometheus:9090"

    # Test with config explicitly set to False
    mock_config.PROMETHEUS_VERIFY_CERTS = False
    collector2 = PrometheusCollector(settings=mock_config)
    collector2._update_url("https://another-prometheus:9090")
    assert collector2.verify is False
