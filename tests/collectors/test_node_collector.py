# tests/collectors/test_node_collector.py

from unittest.mock import MagicMock, patch

from kubernetes import client, config

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


# --- Test Cases ---


@patch("greenkube.collectors.node_collector.config")  # Mock config loading
@patch("greenkube.collectors.node_collector.client.CoreV1Api")  # Mock the K8s client API class
def test_collect_success_with_zones(mock_core_v1_api, mock_k8s_config):
    """
    Tests successful collection when nodes have zone labels.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node("node-1", "us-east-1a"),
            create_mock_node("node-2", "us-west-2b"),
        ]
    )
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert
    assert len(result) == 2
    assert "node-1" in result
    assert "node-2" in result
    assert result["node-1"].zone == "us-east-1a"
    assert result["node-2"].zone == "us-west-2b"
    assert result["node-1"].name == "node-1"
    assert result["node-2"].name == "node-2"
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_partial_zones(mock_core_v1_api, mock_k8s_config):
    """
    Tests collection when some nodes have zone labels and others don't.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node("node-1", "us-east-1a"),
            create_mock_node("node-missing-label"),  # No zone label
        ]
    )
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert
    assert len(result) == 2  # Now includes both nodes
    assert "node-1" in result
    assert "node-missing-label" in result
    assert result["node-1"].zone == "us-east-1a"
    assert result["node-missing-label"].zone is None  # No zone label
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_no_zone_labels(mock_core_v1_api, mock_k8s_config):
    """
    Tests collection when nodes exist but none have the zone label.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(items=[create_mock_node("node-no-label-1"), create_mock_node("node-no-label-2")])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert - now returns ALL nodes, even without zones
    assert len(result) == 2
    assert "node-no-label-1" in result
    assert "node-no-label-2" in result
    assert result["node-no-label-1"].zone is None
    assert result["node-no-label-2"].zone is None
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_no_nodes(mock_core_v1_api, mock_k8s_config):
    """
    Tests collection when the cluster returns no nodes.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(items=[])  # Empty list of nodes
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert
    assert result == {}  # Expect an empty dictionary
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_api_error(mock_core_v1_api, mock_k8s_config):
    """
    Tests that the collector handles Kubernetes API errors gracefully.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    # Simulate an API error when list_node is called
    mock_api_instance.list_node.side_effect = client.ApiException(status=403, reason="Forbidden")

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert
    assert result == {}  # Expect an empty dictionary on API error
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_unexpected_error(mock_core_v1_api, mock_k8s_config):
    """
    Tests that the collector handles unexpected errors during processing.
    """
    # Arrange
    mock_api_instance = mock_core_v1_api.return_value
    # Simulate an unexpected error
    mock_api_instance.list_node.side_effect = Exception("Something went wrong")

    collector = NodeCollector()

    # Act
    result = collector.collect()

    # Assert
    assert result == {}  # Expect an empty dictionary on other errors
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_instance_types_success(mock_core_v1_api, mock_k8s_config):
    """Tests successful collection of instance types when nodes expose the label."""
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node_with_instance("node-1", "m5.large"),
            create_mock_node_with_instance("node-2", "n1-standard-4"),
        ]
    )
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    result = collector.collect_instance_types()

    assert result == {"node-1": "m5.large", "node-2": "n1-standard-4"}
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_instance_types_partial(mock_core_v1_api, mock_k8s_config):
    """Tests collection when some nodes have instance-type labels and others don't."""
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(
        items=[
            create_mock_node_with_instance("node-1", "m5.large"),
            create_mock_node("node-no-instance"),
        ]
    )
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    result = collector.collect_instance_types()

    assert result == {"node-1": "m5.large"}
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_instance_types_no_instances(mock_core_v1_api, mock_k8s_config):
    """Tests collection when no nodes expose instance-type labels."""
    mock_api_instance = mock_core_v1_api.return_value
    mock_node_list = client.V1NodeList(items=[create_mock_node("node-no-label-1"), create_mock_node("node-no-label-2")])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()

    result = collector.collect_instance_types()

    assert result == {}
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_instance_types_api_error(mock_core_v1_api, mock_k8s_config):
    """Tests that API errors are handled gracefully when collecting instance types."""
    mock_api_instance = mock_core_v1_api.return_value
    mock_api_instance.list_node.side_effect = client.ApiException(status=403, reason="Forbidden")

    collector = NodeCollector()

    result = collector.collect_instance_types()

    assert result == {}
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_instance_types_unexpected_error(mock_core_v1_api, mock_k8s_config):
    """Tests that unexpected errors are handled gracefully when collecting instance types."""
    mock_api_instance = mock_core_v1_api.return_value
    mock_api_instance.list_node.side_effect = Exception("boom")

    collector = NodeCollector()

    result = collector.collect_instance_types()

    assert result == {}
    mock_api_instance.list_node.assert_called_once_with(watch=False)


@patch("greenkube.collectors.node_collector.config.load_kube_config")
@patch("greenkube.collectors.node_collector.config.load_incluster_config")
def test_init_failure(mock_load_incluster, mock_load_kube):
    """
    Tests that NodeCollector handles exceptions during __init__ (e.g., config loading)
    by patching the specific load functions.
    """
    # Arrange
    # Make both config loading functions raise the real exception type
    mock_load_incluster.side_effect = config.ConfigException("Incluster failed")
    mock_load_kube.side_effect = config.ConfigException("Kubeconfig failed")

    # Act
    # The collector should not raise but instead gracefully disable cluster access
    nc = NodeCollector()

    # Assert that cluster access was disabled (no API client)
    assert getattr(nc, "v1", None) is None

    # Assert that both mocked functions were called in the expected order
    mock_load_incluster.assert_called_once()
    mock_load_kube.assert_called_once()


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


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_detailed_info_ovh(mock_core_v1_api, mock_k8s_config):
    """Test detailed info collection for OVH nodes."""
    mock_api_instance = mock_core_v1_api.return_value

    ovh_labels = {
        "k8s.ovh.net/nodepool": "019841a6-24bd-7e0e-b35f-86bfe7d13189",
        "node.kubernetes.io/instance-type": "b3-8",
        "topology.kubernetes.io/zone": "eu-west-par-a",
        "topology.kubernetes.io/region": "EU-WEST-PAR",
        "kubernetes.io/arch": "amd64",
    }

    mock_node_list = client.V1NodeList(items=[create_mock_node_detailed("ovh-node-1", ovh_labels)])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()
    result = collector.collect_detailed_info()

    assert "ovh-node-1" in result
    node_info = result["ovh-node-1"]
    assert node_info["cloud_provider"] == "ovh"
    assert node_info["instance_type"] == "b3-8"
    assert node_info["zone"] == "eu-west-par-a"
    assert node_info["region"] == "EU-WEST-PAR"
    assert node_info["architecture"] == "amd64"
    assert node_info["node_pool"] == "019841a6-24bd-7e0e-b35f-86bfe7d13189"


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_detailed_info_azure(mock_core_v1_api, mock_k8s_config):
    """Test detailed info collection for Azure AKS nodes."""
    mock_api_instance = mock_core_v1_api.return_value

    azure_labels = {
        "kubernetes.azure.com/agentpool": "tipool",
        "kubernetes.azure.com/cluster": "MC_rg-berlese-aks_Cluster-AKS-TI_francecentral",
        "node.kubernetes.io/instance-type": "Standard_D2ps_v6",
        "topology.kubernetes.io/zone": "francecentral-1",
        "topology.kubernetes.io/region": "francecentral",
        "kubernetes.io/arch": "arm64",
    }

    mock_node_list = client.V1NodeList(items=[create_mock_node_detailed("azure-node-1", azure_labels, "2")])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()
    result = collector.collect_detailed_info()

    assert "azure-node-1" in result
    node_info = result["azure-node-1"]
    assert node_info["cloud_provider"] == "azure"
    assert node_info["instance_type"] == "Standard_D2ps_v6"
    assert node_info["zone"] == "francecentral-1"
    assert node_info["region"] == "francecentral"
    assert node_info["architecture"] == "arm64"
    assert node_info["node_pool"] == "tipool"


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_detailed_info_unknown_provider(mock_core_v1_api, mock_k8s_config):
    """Test detailed info collection for nodes with unknown cloud provider."""
    mock_api_instance = mock_core_v1_api.return_value

    generic_labels = {
        "node.kubernetes.io/instance-type": "generic-4cpu",
        "topology.kubernetes.io/zone": "zone-a",
        "topology.kubernetes.io/region": "region-1",
        "kubernetes.io/arch": "amd64",
    }

    mock_node_list = client.V1NodeList(items=[create_mock_node_detailed("generic-node", generic_labels)])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()
    result = collector.collect_detailed_info()

    assert "generic-node" in result
    node_info = result["generic-node"]
    assert node_info["cloud_provider"] == "unknown"
    assert node_info["instance_type"] == "generic-4cpu"
    assert node_info["node_pool"] is None


@patch("greenkube.collectors.node_collector.config")
@patch("greenkube.collectors.node_collector.client.CoreV1Api")
def test_collect_detailed_info_inferred_instance_type(mock_core_v1_api, mock_k8s_config):
    """Test that instance type is inferred from CPU capacity when labels are missing."""
    mock_api_instance = mock_core_v1_api.return_value

    minimal_labels = {
        "topology.kubernetes.io/zone": "zone-a",
        "kubernetes.io/arch": "amd64",
    }

    mock_node_list = client.V1NodeList(items=[create_mock_node_detailed("inferred-node", minimal_labels, "8")])
    mock_api_instance.list_node.return_value = mock_node_list

    collector = NodeCollector()
    result = collector.collect_detailed_info()

    assert "inferred-node" in result
    node_info = result["inferred-node"]
    assert node_info["instance_type"] == "cpu-8"
