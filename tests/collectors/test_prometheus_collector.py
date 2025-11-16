# tests/collectors/test_prometheus_collector.py
"""
Tests for the PrometheusCollector using Test-Driven Development (TDD).

We will mock all HTTP requests to the Prometheus API.
"""

import urllib.parse
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import requests
from requests_mock import ANY

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
    return config


@pytest.fixture
def collector(mock_config):
    """Return a PrometheusCollector instance with a mocked config."""
    return PrometheusCollector(settings=mock_config)


# --- Test Cases ---


def test_collect_success(collector, requests_mock):
    """
    Test the happy path: Prometheus is reachable and returns valid data.
    """
    mock_url = collector.base_url
    # Note: the query has changed, requests_mock will find it dynamically
    query_params_cpu = {"query": collector.cpu_usage_query}
    url_cpu = f"{mock_url}/api/v1/query?{urllib.parse.urlencode(query_params_cpu)}"
    requests_mock.get(url_cpu, json=MOCK_CPU_USAGE_RESPONSE)

    query_params_node = {"query": collector.node_labels_query}
    url_node = f"{mock_url}/api/v1/query?{urllib.parse.urlencode(query_params_node)}"
    requests_mock.get(url_node, json=MOCK_NODE_LABELS_RESPONSE)

    result = collector.collect()

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


@patch.object(PrometheusDiscovery, "discover")
def test_collect_with_no_url_triggers_discovery(mock_discover, collector, requests_mock):
    """
    Test that collect() triggers discovery when PROMETHEUS_URL is not set.
    """
    # We need a collector with no URL
    collector.base_url = None

    # 1. Discovery is mocked to return a new URL
    discovered_url = "http://discovered-prometheus:9090"
    mock_discover.return_value = discovered_url

    # 2. Mock the API calls to the *discovered* URL. The initial query with a
    #    blank URL will fail, triggering discovery.
    requests_mock.get(
        f"{discovered_url}/api/v1/query",
        [
            {"json": MOCK_CPU_USAGE_RESPONSE},
            {"json": MOCK_NODE_LABELS_RESPONSE},
        ],
    )

    # 3. Call collect - this should now succeed via discovery
    result = collector.collect()

    # 4. Assertions
    mock_discover.assert_called_once()
    assert collector.base_url == discovered_url
    assert len(result.pod_cpu_usage) == 2
    assert len(result.node_instance_types) == 2


def test_collect_connection_error(collector, requests_mock):
    """
    Test behavior when Prometheus is unreachable.
    """
    requests_mock.get(ANY, exc=requests.exceptions.ConnectionError("Connection refused"))

    result = collector.collect()

    # Should fail gracefully and return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


def test_collect_api_error(collector, requests_mock):
    """
    Test behavior when Prometheus returns an HTTP 500 or other error.
    """
    requests_mock.get(ANY, status_code=500, json={"status": "error", "error": "Internal error"})

    result = collector.collect()

    # Should fail gracefully and return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


def test_collect_empty_results(collector, requests_mock):
    """
    Test behavior when Prometheus is reachable but returns no data.
    """
    requests_mock.get(ANY, json=MOCK_EMPTY_RESPONSE)

    result = collector.collect()

    # Should return an empty data object
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0


def test_parsing_malformed_cpu_data(collector):
    """
    Test that malformed data is gracefully handled and returns None.
    """
    # This mock data has a string for value, but it's not a float
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


def test_collect_ignores_non_pod_series(collector, requests_mock):
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
    url_cpu = f"{mock_url}/api/v1/query?{urllib.parse.urlencode(query_params_cpu)}"
    requests_mock.get(url_cpu, json=mixed_result)

    # Node labels empty to avoid interfering
    query_params_node = {"query": collector.node_labels_query}
    url_node = f"{mock_url}/api/v1/query?{urllib.parse.urlencode(query_params_node)}"
    requests_mock.get(url_node, json=MOCK_EMPTY_RESPONSE)

    result = collector.collect()
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


@patch.object(PrometheusCollector, "is_available")
def test_collect_range_with_no_url_triggers_discovery(mock_is_available, collector, requests_mock):
    """
    Test that collect_range() triggers discovery when PROMETHEUS_URL is not set.
    """
    # We need a collector with no URL
    collector.base_url = None
    discovered_url = "http://discovered-prometheus:9090"

    def side_effect():
        collector.base_url = discovered_url
        return True

    mock_is_available.side_effect = side_effect

    # Mock the API call to the discovered URL
    # The query_range endpoint has a different structure for its query params
    # We will use a matcher to catch the request regardless of the exact query params
    requests_mock.get(f"{discovered_url}/api/v1/query_range", json=MOCK_CPU_RANGE_RESPONSE)

    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)
    result = collector.collect_range(start_time, end_time)

    mock_is_available.assert_called_once()
    assert collector.base_url == discovered_url
    assert len(result) == 1
    assert result[0]["metric"]["pod"] == "api-1"
