# tests/collectors/test_prometheus_collector.py
"""
Tests for the PrometheusCollector using Test-Driven Development (TDD).

We will mock all HTTP requests to the Prometheus API.
"""
import pytest
import requests
from requests_mock import ANY
from pydantic import ValidationError

from greenkube.core.config import Config
from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.models.prometheus_metrics import PrometheusMetric, PodCPUUsage, NodeInstanceType

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
                    "pod": "api-deployment-12345"
                },
                "value": [1678886400, "0.5"] # 0.5 cores
            },
            {
                "metric": {
                    "container": "db",
                    "namespace": "prod",
                    "pod": "db-deployment-67890"
                },
                "value": [1678886400, "1.2"] # 1.2 cores
            },
            {
                "metric": {
                    # Missing 'pod' label, should be skipped
                    "container": "sidecar",
                    "namespace": "default"
                },
                "value": [1678886400, "0.1"]
            },
            {
                "metric": {
                    "container": "nan-value-container",
                    "namespace": "prod",
                    "pod": "api-deployment-99999"
                },
                "value": [1678886400, "NaN"] # Should be skipped
            }
        ]
    }
}

MOCK_NODE_LABELS_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {
                    "node": "node-1",
                    "label_node_kubernetes_io_instance_type": "m5.large"
                },
                "value": [1678886400, "1"]
            },
            {
                "metric": {
                    "node": "node-2",
                    "label_node_kubernetes_io_instance_type": "t3.medium"
                },
                "value": [1678886400, "1"]
            },
            {
                "metric": {
                    # Missing instance type label, should be skipped
                    "node": "node-3"
                },
                "value": [1678886400, "1"]
            }
        ]
    }
}

MOCK_EMPTY_RESPONSE = {"status": "success", "data": {"resultType": "vector", "result": []}}

# --- Pytest Fixtures ---

@pytest.fixture
def mock_config(monkeypatch):
    """Fixture to create a Config instance and patch its attributes for tests."""
    monkeypatch.setattr(Config, "PROMETHEUS_URL", "http://mock-prometheus:9090")
    monkeypatch.setattr(Config, "PROMETHEUS_QUERY_RANGE_STEP", "5m")
    return Config()

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
    requests_mock.get(f"{mock_url}/api/v1/query?query={collector.cpu_usage_query}", json=MOCK_CPU_USAGE_RESPONSE)
    requests_mock.get(f"{mock_url}/api/v1/query?query={collector.node_labels_query}", json=MOCK_NODE_LABELS_RESPONSE)

    result = collector.collect()

    assert isinstance(result, PrometheusMetric)
    
    # Check CPU data (2 valid, 2 invalid entries in mock)
    assert len(result.pod_cpu_usage) == 2
    assert result.pod_cpu_usage[0].namespace == "prod"
    assert result.pod_cpu_usage[0].pod == "api-deployment-12345"
    assert result.pod_cpu_usage[0].container == "app"
    assert result.pod_cpu_usage[0].cpu_usage_cores == 0.5
    assert result.pod_cpu_usage[1].cpu_usage_cores == 1.2

    # Check Node data (2 valid, 1 invalid entry in mock)
    assert len(result.node_instance_types) == 2
    assert result.node_instance_types[0].node == "node-1"
    assert result.node_instance_types[0].instance_type == "m5.large"
    assert result.node_instance_types[1].node == "node-2"
    assert result.node_instance_types[1].instance_type == "t3.medium"

def test_collect_no_url_configured(monkeypatch):
    """
    Test behavior when PROMETHEUS_URL is not set.
    """
    monkeypatch.setattr(Config, "PROMETHEUS_URL", None)
    settings = Config()
    collector = PrometheusCollector(settings=settings)
    
    result = collector.collect()
    
    # Should return an empty data object and not try to connect
    assert isinstance(result, PrometheusMetric)
    assert len(result.pod_cpu_usage) == 0
    assert len(result.node_instance_types) == 0

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
    malformed_cpu_data = {"metric": {"namespace": "n", "pod": "p", "container": "c"}, "value": [0, "not-a-float"]}
    
    assert collector._parse_cpu_data(malformed_cpu_data) is None

def test_parsing_missing_cpu_labels(collector):
    """
    Test that the parser returns None if key labels are missing.
    """
    # Missing 'pod'
    missing_label_data = {"metric": {"namespace": "n", "container": "c"}, "value": [0, "0.5"]}
    assert collector._parse_cpu_data(missing_label_data) is None

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
    nan_data = {"metric": {"namespace": "n", "pod": "p", "container": "c"}, "value": [0, "NaN"]}
    assert collector._parse_cpu_data(nan_data) is None
