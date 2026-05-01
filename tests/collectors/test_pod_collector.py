# tests/collectors/test_pod_collector.py

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import models as k8s

from greenkube.collectors.pod_collector import PodCollector
from greenkube.utils.k8s_utils import parse_cpu_request, parse_memory_request, parse_storage_request


# Fixture to simulate the Kubernetes API
@pytest.fixture
def mock_k8s_api():
    """Mock of the Kubernetes CoreV1Api."""
    mock_api = MagicMock()

    # Define resources for containers
    resources_1 = k8s.V1ResourceRequirements(requests={"cpu": "500m", "memory": "1Gi"})
    resources_2 = k8s.V1ResourceRequirements(requests={"cpu": "100m", "memory": "256Mi"})
    # Container with no requests
    resources_3 = k8s.V1ResourceRequirements(requests=None)

    # Container with only CPU
    resources_4 = k8s.V1ResourceRequirements(requests={"cpu": "200m"})

    # Create containers
    container_1 = k8s.V1Container(name="app-container", resources=resources_1)
    container_2 = k8s.V1Container(name="sidecar-container", resources=resources_2)
    container_3 = k8s.V1Container(name="no-request-container", resources=resources_3)
    container_4 = k8s.V1Container(name="cpu-only-container", resources=resources_4)

    # Create pods
    pod_1 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-1", namespace="prod"),
        spec=k8s.V1PodSpec(containers=[container_1, container_2]),
    )
    pod_2 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-2", namespace="dev"),
        spec=k8s.V1PodSpec(containers=[container_3]),
    )
    pod_3 = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="app-pod-3", namespace="dev"),
        spec=k8s.V1PodSpec(containers=[container_4]),
    )

    # Simulate the API response
    # For async client, return_value should be awaitable or AsyncMock
    mock_api.list_pod_for_all_namespaces = AsyncMock(return_value=k8s.V1PodList(items=[pod_1, pod_2, pod_3]))

    return mock_api


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_pod_collector_success(mock_get_api, mock_k8s_api):
    """Tests the successful collection of pod and container requests."""
    # Patch the k8s client to return our mock
    # mock_load_incluster.side_effect = AsyncMock() # Removed: config loading is sync
    mock_get_api.return_value = mock_k8s_api

    collector = PodCollector()
    results = await collector.collect()

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
    assert metric_4.cpu_request == 200  # 200m
    assert metric_4.memory_request == 0  # No memory request


@pytest.mark.asyncio
@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_pod_collector_api_error(mock_get_api, mock_k8s_api):
    """Tests exception handling during the K8s API call."""
    # mock_load_incluster.side_effect = AsyncMock() # Removed
    mock_get_api.return_value = mock_k8s_api
    mock_k8s_api.list_pod_for_all_namespaces.side_effect = Exception("K8s API Error")

    collector = PodCollector()
    results = await collector.collect()

    # The collector should catch the error and return an empty list
    assert results == []


def test_parse_cpu_units():
    """Tests the helper function for parsing CPU units (now in k8s_utils)."""
    # Simulate an instance to test the method - wait, we moved it to k8s_utils
    # But checking if we want to keep unit tests here or distinct.
    # Since PodCollector uses them, verifying behavior is fine, but we should use the imported functions directly.

    assert parse_cpu_request("1") == 1000  # 1 core
    assert parse_cpu_request("500m") == 500  # 500 millicores
    assert parse_cpu_request("0.25") == 250  # 0.25 cores
    assert parse_cpu_request(None) == 0
    assert parse_cpu_request("10n") == 0  # Nano-cores ~0 for integer millicores (10n = 0.00000001)


def test_parse_memory_units():
    """Tests the helper function for parsing Memory units."""

    assert parse_memory_request("1Ki") == 1024
    assert parse_memory_request("1Mi") == 1024 * 1024
    assert parse_memory_request("1Gi") == 1024 * 1024 * 1024
    assert parse_memory_request("1Ti") == 1024 * 1024 * 1024 * 1024
    assert parse_memory_request("100") == 100  # Bytes
    assert parse_memory_request(None) == 0
    assert parse_memory_request("1G") == 1000 * 1000 * 1000  # G (non-binary)
    assert parse_memory_request("1M") == 1000 * 1000  # M (non-binary)


@pytest.mark.asyncio
async def test_pod_collector_returns_empty_without_client(monkeypatch):
    collector = PodCollector()
    monkeypatch.setattr(collector, "_ensure_client", AsyncMock(return_value=None))

    assert await collector.collect() == []


@pytest.mark.asyncio
async def test_pod_collector_reuses_existing_client():
    collector = PodCollector()
    collector._api = MagicMock()

    assert await collector._ensure_client() is collector._api


@pytest.mark.asyncio
@patch("greenkube.collectors.pod_collector.get_core_v1_api")
async def test_pod_collector_handles_missing_client(mock_get_api):
    mock_get_api.return_value = None
    collector = PodCollector()

    assert await collector._ensure_client() is None
    assert await collector.collect() == []


@pytest.mark.asyncio
async def test_pod_collector_extracts_owner_and_ephemeral_storage():
    replica_set_owner = k8s.V1OwnerReference(
        api_version="apps/v1",
        kind="ReplicaSet",
        name="api-abc123",
        uid="1",
        controller=True,
    )
    non_controller_owner = k8s.V1OwnerReference(
        api_version="batch/v1",
        kind="Job",
        name="ignored",
        uid="2",
        controller=False,
    )
    container = k8s.V1Container(
        name="app",
        resources=k8s.V1ResourceRequirements(requests={"cpu": "1", "memory": "128Mi", "ephemeral-storage": "1Gi"}),
    )
    owned_pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(
            name="api-pod",
            namespace="prod",
            owner_references=[non_controller_owner, replica_set_owner],
        ),
        spec=k8s.V1PodSpec(containers=[container]),
    )
    no_spec_pod = k8s.V1Pod(metadata=k8s.V1ObjectMeta(name="empty", namespace="prod"), spec=None)
    no_container_pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name="empty-list", namespace="prod"),
        spec=k8s.V1PodSpec(containers=[]),
    )
    api = MagicMock()
    api.list_pod_for_all_namespaces = AsyncMock(
        return_value=k8s.V1PodList(items=[owned_pod, no_spec_pod, no_container_pod])
    )
    collector = PodCollector()
    collector._api = api

    metrics = await collector.collect()

    assert len(metrics) == 1
    assert metrics[0].owner_kind == "Deployment"
    assert metrics[0].owner_name == "api"
    assert metrics[0].ephemeral_storage_request == 1024**3


def test_parse_storage_units():
    assert parse_storage_request("1Mi") == 1024 * 1024
    assert parse_storage_request("1G") == 1000 * 1000 * 1000


@pytest.mark.asyncio
async def test_pod_collector_close_closes_cached_api_client():
    collector = PodCollector()
    api_client = MagicMock()
    api_client.close = AsyncMock()
    collector._api = MagicMock(api_client=api_client)

    await collector.close()

    api_client.close.assert_awaited_once()
    assert collector._api is None
