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
    assert result == {"node-1": "us-east-1a", "node-2": "us-west-2b"}
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
    assert result == {"node-1": "us-east-1a"}  # Only node-1 should be included
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

    # Assert
    assert result == {}  # Expect an empty dictionary
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
