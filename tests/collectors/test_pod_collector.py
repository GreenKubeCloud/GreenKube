# tests/collectors/test_pod_collector.py

import pytest
from unittest.mock import MagicMock, patch
from kubernetes.client import models as k8s
from greenkube.collectors.pod_collector import PodCollector
from greenkube.models.metrics import PodMetric

# Fixture to simulate the Kubernetes API
@pytest.fixture
def mock_k8s_api():
    """Mock of the Kubernetes CoreV1Api."""
    mock_api = MagicMock()

    # Define resources for containers
    resources_1 = k8s.V1ResourceRequirements(
        requests={"cpu": "500m", "memory": "1Gi"}
    )
    resources_2 = k8s.V1ResourceRequirements(
        requests={"cpu": "100m", "memory": "256Mi"}
    )
    # Container with no requests
    resources_3 = k8s.V1ResourceRequirements(requests=None)
    
    # Container with only CPU
    resources_4 = k8s.V1ResourceRequirements(
        requests={"cpu": "200m"}
    )

    # Create containers
    container_1 = k8s.V1Container(name="app-container", resources=resources_1)
    container_2 = k8s.V1Container(name="sidecar-container", resources=resources_2)
    container_3 = k8s.V1Container(name="no-request-container", resources=resources_3)
    container_4 = k8s.V1Container(name="cpu-only-container", resources=resources_4)

    # Create pods
    pod_1 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-1", namespace="prod"),
        spec=k8s.V1PodSpec(containers=[container_1, container_2])
    )
    pod_2 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-2", namespace="dev"),
        spec=k8s.V1PodSpec(containers=[container_3])
    )
    pod_3 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-3", namespace="dev"),
        spec=k8s.V1PodSpec(containers=[container_4])
    )

    # Simulate the API response
    mock_api.list_pod_for_all_namespaces.return_value = k8s.V1PodList(
        items=[pod_1, pod_2, pod_3]
    )
    
    return mock_api

def test_pod_collector_success(mock_k8s_api):
    """Tests the successful collection of pod and container requests."""
    # Patch the k8s client to return our mock
    with patch("kubernetes.client.CoreV1Api", return_value=mock_k8s_api):
        collector = PodCollector()
        results = collector.collect()

        assert len(results) == 4  # One PodMetric per container

        # Check Pod 1, Container 1
        metric_1 = next(m for m in results if m.container_name == "app-container")
        assert metric_1.pod_name == "app-pod-1"
        assert metric_1.namespace == "prod"
        assert metric_1.cpu_request == 500  # 500m
        assert metric_1.memory_request == 1073741824  # 1Gi

        # Check Pod 1, Container 2
        metric_2 = next(m for m in results if m.container_name == "sidecar-container")
        assert metric_2.pod_name == "app-pod-1"
        assert metric_2.namespace == "prod"
        assert metric_2.cpu_request == 100  # 100m
        assert metric_2.memory_request == 268435456  # 256Mi

        # Check Pod 2, Container 3 (no requests)
        metric_3 = next(m for m in results if m.container_name == "no-request-container")
        assert metric_3.pod_name == "app-pod-2"
        assert metric_3.namespace == "dev"
        assert metric_3.cpu_request == 0
        assert metric_3.memory_request == 0
        
        # Check Pod 3, Container 4 (CPU only)
        metric_4 = next(m for m in results if m.container_name == "cpu-only-container")
        assert metric_4.pod_name == "app-pod-3"
        assert metric_4.namespace == "dev"
        assert metric_4.cpu_request == 200 # 200m
        assert metric_4.memory_request == 0 # No memory request

def test_pod_collector_api_error(mock_k8s_api):
    """Tests exception handling during the K8s API call."""
    mock_k8s_api.list_pod_for_all_namespaces.side_effect = Exception("K8s API Error")
    
    with patch("kubernetes.client.CoreV1Api", return_value=mock_k8s_api):
        collector = PodCollector()
        results = collector.collect()
        
        # The collector should catch the error and return an empty list
        assert results == []

def test_parse_cpu_units():
    """Tests the (private) helper function for parsing CPU units."""
    # Simulate an instance to test the method
    collector = PodCollector()
    
    assert collector._parse_cpu_request("1") == 1000  # 1 core
    assert collector._parse_cpu_request("500m") == 500  # 500 millicores
    assert collector._parse_cpu_request("0.25") == 250  # 0.25 cores
    assert collector._parse_cpu_request(None) == 0
    assert collector._parse_cpu_request("10n") == 0 # Nano-cores not supported (too small)

def test_parse_memory_units():
    """Tests the (private) helper function for parsing Memory units."""
    collector = PodCollector()
    
    assert collector._parse_memory_request("1Ki") == 1024
    assert collector._parse_memory_request("1Mi") == 1024 * 1024
    assert collector._parse_memory_request("1Gi") == 1024 * 1024 * 1024
    assert collector._parse_memory_request("1Ti") == 1024 * 1024 * 1024 * 1024
    assert collector._parse_memory_request("100") == 100 # Bytes
    assert collector._parse_memory_request(None) == 0
    assert collector._parse_memory_request("1G") == 1000 * 1000 * 1000 # G (non-binary)
    assert collector._parse_memory_request("1M") == 1000 * 1000 # M (non-binary)

