# tests/collectors/test_node_collector.py

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio import client

# We need to import the module where ConfigException lives to patch effectively if we were mocking it,
# but here we just want to avoid mocking the class itself.
from kubernetes_asyncio import config as k8s_config

from greenkube.collectors.node_collector import NodeCollector

# --- Mock Kubernetes Objects ---


def create_mock_node(name, zone_label_value=None):
    """Helper function to create a mock V1Node object."""
    node = MagicMock(spec=client.V1Node)
    node.metadata = MagicMock(spec=client.V1ObjectMeta)
    node.metadata.name = name
    node.metadata.labels = {}
    if zone_label_value:
        node.metadata.labels["topology.kubernetes.io/zone"] = zone_label_value
    return node


def create_mock_node_with_instance(name, instance_label_value=None):
    """Helper to create a mock V1Node with an instance-type label."""
    node = MagicMock(spec=client.V1Node)
    node.metadata = MagicMock(spec=client.V1ObjectMeta)
    node.metadata.name = name
    node.metadata.labels = {}
    if instance_label_value:
        node.metadata.labels["label_node_kubernetes_io_instance_type"] = instance_label_value
    return node


def create_mock_node_detailed(name, labels, capacity_cpu="4"):
    """Helper to create a detailed mock node with labels and capacity."""
    node = MagicMock(spec=client.V1Node)
    node.metadata = MagicMock(spec=client.V1ObjectMeta)
    node.metadata.name = name
    node.metadata.labels = labels

    # Mock status and capacity
    node.status = MagicMock()
    node.status.capacity = {"cpu": capacity_cpu}

    return node


# --- Test Cases ---


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_success_with_zones(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests successful collection when nodes have zone labels.
    """
    # Arrange
    # Mock config loading to succeed (incluster first)
    mock_load_incluster.return_value = (
        None  # AsyncMock by default? patch makes it MagicMock/AsyncMock if target is async?
    )
    # Actually config.load_incluster_config is async. patch usually creates a MagicMock.
    # We should ensure it's awaited properly or use AsyncMock if needed, but MagicMock works if return_value is
    # awaitable or if we set it as AsyncMock.
    # However, since we are patching the function on that module which imports 'config' from 'kubernetes_asyncio',
    mock_load_incluster.side_effect = AsyncMock()

    # Mock CoreV1Api instance
    mock_api_instance = mock_core_v1_api.return_value

    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node("node-1", "us-east-1a"),
            create_mock_node("node-2", "us-west-2b"),
        ]
    )
    # Using AsyncMock for list_node
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert len(result) == 2
    assert "node-1" in result
    assert "node-2" in result
    assert result["node-1"].zone == "us-east-1a"
    assert result["node-2"].zone == "us-west-2b"


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_partial_zones(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests collection when some nodes have zone labels and others don't.
    """
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node("node-1", "us-east-1a"),
            create_mock_node("node-missing-label"),
        ]
    )
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert len(result) == 2
    assert "node-1" in result
    assert "node-missing-label" in result
    assert result["node-1"].zone == "us-east-1a"
    assert result["node-missing-label"].zone is None


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_no_zone_labels(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests collection when nodes exist but none have the zone label.
    """
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(items=[create_mock_node("node-no-label-1"), create_mock_node("node-no-label-2")])
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert len(result) == 2
    assert "node-no-label-1" in result
    assert result["node-no-label-1"].zone is None


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_no_nodes(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests collection when the cluster returns no nodes.
    """
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(items=[])
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_api_error(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests that the collector handles Kubernetes API errors gracefully.
    """
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value
    # AsyncMock raising exception. Note the import of ApiException from rest in collector
    from kubernetes_asyncio.client.rest import ApiException

    mock_api_instance.list_node = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_unexpected_error(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests that the collector handles unexpected errors during processing.
    """
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value
    mock_api_instance.list_node = AsyncMock(side_effect=Exception("Something went wrong"))

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_collect_detailed_info_ovh(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """Test detailed info collection for OVH nodes."""
    mock_load_incluster.side_effect = AsyncMock()
    mock_api_instance = mock_core_v1_api.return_value

    ovh_labels = {
        "k8s.ovh.net/nodepool": "019841a6-24bd-7e0e-b35f-86bfe7d13189",
        "node.kubernetes.io/instance-type": "b3-8",
        "topology.kubernetes.io/zone": "eu-west-par-a",
        "topology.kubernetes.io/region": "EU-WEST-PAR",
        "kubernetes.io/arch": "amd64",
    }

    mock_node_list = client.V1NodeList(items=[create_mock_node_detailed("ovh-node-1", ovh_labels)])
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()
    result = await collector.collect_detailed_info()

    assert "ovh-node-1" in result
    node_info = result["ovh-node-1"]
    assert node_info["cloud_provider"] == "ovh"
    assert node_info["instance_type"] == "b3-8"


@pytest.mark.asyncio
@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
async def test_init_fails_gracefully(mock_core_v1_api, mock_load_incluster, mock_load_kube):
    """
    Tests that if both config loading methods fail, we handle it.
    """
    # Assuming k8s_config.ConfigException is available
    mock_load_incluster.side_effect = k8s_config.ConfigException("Fail")
    mock_load_kube.side_effect = k8s_config.ConfigException("Fail")

    collector = NodeCollector()

    # Act
    # Calling collect with failed config
    result = await collector.collect()

    # Assert
    assert result == {}
