# tests/collectors/test_node_collector.py

from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio import client

# We need to import the module where ConfigException lives to patch effectively if we were mocking it,
# but here we just want to avoid mocking the class itself.
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


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_success_with_zones(mock_get_api):
    """
    Tests successful collection when nodes have zone labels.
    """
    # Arrange
    # mock_load_kube is top patch (load_kube_config) for decorator, but unittest patch applies:
    # 1. client.CoreV1Api -> passed as 1st arg if we use decorator order?
    # NO. @patch decorator passes mock as argument.
    # @patch(A) @patch(B) def test(mock_A, mock_B)
    # So: top patch = 1st arg.
    # Top: load_kube_config -> mock_core_v1_api (var name) -> matches
    # Middle: load_incluster_config -> mock_load_incluster -> matches
    # Bottom: CoreV1Api -> mock_load_kube (var name) -> incorrect name

    # Let's verify usage:
    # We want get_core_v1_api to return the mock instance
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance

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


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_partial_zones(mock_get_api):
    """
    Tests collection when some nodes have zone labels and others don't.
    """
    # Patches: 1. load_kube (mock_load_kube), 2. load_incluster (mock_load_incluster), 3. CoreV1Api (mock_core_v1_api)
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance
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


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_no_zone_labels(mock_get_api):
    """
    Tests collection when nodes exist but none have the zone label.
    """
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance
    mock_node_list = client.V1NodeList(items=[create_mock_node("node-no-label-1"), create_mock_node("node-no-label-2")])
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert len(result) == 2
    assert "node-no-label-1" in result
    assert result["node-no-label-1"].zone is None


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_no_nodes(mock_get_api):
    """
    Tests collection when the cluster returns no nodes.
    """
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance
    mock_node_list = client.V1NodeList(items=[])
    mock_api_instance.list_node = AsyncMock(return_value=mock_node_list)

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_api_error(mock_get_api):
    """
    Tests that the collector handles Kubernetes API errors gracefully.
    """
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance
    # AsyncMock raising exception. Note the import of ApiException from rest in collector
    from kubernetes_asyncio.client.rest import ApiException

    mock_api_instance.list_node = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_unexpected_error(mock_get_api):
    """
    Tests that the collector handles unexpected errors during processing.
    """
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance
    mock_api_instance.list_node = AsyncMock(side_effect=Exception("Something went wrong"))

    collector = NodeCollector()

    # Act
    result = await collector.collect()

    # Assert
    assert result == {}


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_detailed_info_ovh(mock_get_api):
    """Test detailed info collection for OVH nodes."""
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance

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


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_init_fails_gracefully(mock_get_api):
    """
    Tests that if both config loading methods fail, we handle it.
    """
    # Patches: 1. load_kube (mock_load_kube), 2. load_incluster (mock_load_incluster), 3. CoreV1Api (mock_core_v1_api)
    # Assuming k8s_config.ConfigException is available
    # Simulate failure to get API client (returns None)
    mock_get_api.return_value = None

    collector = NodeCollector()

    # Act
    # Calling collect with failed config
    result = await collector.collect()

    # Assert
    assert result == {}


# ---------------------------------------------------------------------------
# Scaleway detection tests
# ---------------------------------------------------------------------------


def _make_scaleway_node(name: str, labels: dict, provider_id: str = "", capacity_cpu: str = "4"):
    """Create a mock node with Scaleway-style metadata."""
    node = MagicMock(spec=client.V1Node)
    node.metadata = MagicMock(spec=client.V1ObjectMeta)
    node.metadata.name = name
    node.metadata.labels = labels
    node.spec = MagicMock()
    node.spec.provider_id = provider_id
    node.status = MagicMock()
    node.status.capacity = {"cpu": capacity_cpu}
    return node


def test_detect_cloud_provider_scaleway_via_label():
    """_detect_cloud_provider returns 'scaleway' when k8s.scaleway.com/* labels are present."""
    collector = NodeCollector()
    labels = {
        "k8s.scaleway.com/nodepool-id": "abc-123",
        "k8s.scaleway.com/nodepool-name": "default",
        "node.kubernetes.io/instance-type": "DEV1-M",
        "topology.kubernetes.io/zone": "fr-par-1",
    }
    assert collector._detect_cloud_provider(labels) == "scaleway"


def test_detect_cloud_provider_scaleway_via_provider_id():
    """_detect_cloud_provider falls back to providerID when no Scaleway labels exist."""
    collector = NodeCollector()
    labels = {
        "node.kubernetes.io/instance-type": "DEV1-M",
        "topology.kubernetes.io/zone": "fr-par-1",
    }
    node = _make_scaleway_node("scw-node", labels, provider_id="scaleway://instance/fr-par-1/uuid-1234")
    assert collector._detect_cloud_provider(labels, node) == "scaleway"


def test_detect_cloud_provider_unknown_without_node():
    """_detect_cloud_provider returns 'unknown' with no cloud-specific labels and no node."""
    collector = NodeCollector()
    labels = {"kubernetes.io/hostname": "my-node"}
    assert collector._detect_cloud_provider(labels) == "unknown"


def test_extract_node_pool_scaleway_name_label():
    """_extract_node_pool returns the pool name from k8s.scaleway.com/nodepool-name."""
    collector = NodeCollector()
    labels = {
        "k8s.scaleway.com/nodepool-name": "my-pool",
        "k8s.scaleway.com/nodepool-id": "pool-uuid-1234",
    }
    assert collector._extract_node_pool(labels, "scaleway") == "my-pool"


def test_extract_node_pool_scaleway_id_fallback():
    """_extract_node_pool falls back to nodepool-id when nodepool-name is absent."""
    collector = NodeCollector()
    labels = {"k8s.scaleway.com/nodepool-id": "pool-uuid-1234"}
    assert collector._extract_node_pool(labels, "scaleway") == "pool-uuid-1234"


def test_extract_node_pool_scaleway_none_when_no_labels():
    """_extract_node_pool returns None when Scaleway pool labels are absent."""
    collector = NodeCollector()
    assert collector._extract_node_pool({}, "scaleway") is None


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_scaleway_node_full(mock_get_api):
    """collect() correctly identifies a full Scaleway Kapsule node."""
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance

    scaleway_labels = {
        "k8s.scaleway.com/nodepool-id": "pool-abc123",
        "k8s.scaleway.com/nodepool-name": "default",
        "node.kubernetes.io/instance-type": "DEV1-M",
        "topology.kubernetes.io/zone": "fr-par-1",
        "topology.kubernetes.io/region": "fr-par",
        "kubernetes.io/arch": "amd64",
    }
    scw_node = _make_scaleway_node(
        "scw-kapsule-node-1",
        scaleway_labels,
        provider_id="scaleway://instance/fr-par-1/uuid-deadbeef",
    )

    mock_api_instance.list_node = AsyncMock(return_value=client.V1NodeList(items=[scw_node]))

    collector = NodeCollector()
    result = await collector.collect()

    assert "scw-kapsule-node-1" in result
    node_info = result["scw-kapsule-node-1"]
    assert node_info.cloud_provider == "scaleway"
    assert node_info.instance_type == "DEV1-M"
    assert node_info.zone == "fr-par-1"
    assert node_info.region == "fr-par"
    assert node_info.node_pool == "default"
    assert node_info.architecture == "amd64"


@patch("greenkube.collectors.node_collector.get_core_v1_api")
async def test_collect_scaleway_node_providerid_fallback(mock_get_api):
    """collect() detects Scaleway via providerID when k8s.scaleway.com labels are absent."""
    mock_api_instance = AsyncMock()
    mock_get_api.return_value = mock_api_instance

    # Minimal labels — no k8s.scaleway.com/* present
    labels = {
        "node.kubernetes.io/instance-type": "GP1-XS",
        "topology.kubernetes.io/zone": "nl-ams-1",
        "topology.kubernetes.io/region": "nl-ams",
    }
    scw_node = _make_scaleway_node(
        "scw-node-minimal",
        labels,
        provider_id="scaleway://instance/nl-ams-1/uuid-cafebabe",
    )

    mock_api_instance.list_node = AsyncMock(return_value=client.V1NodeList(items=[scw_node]))

    collector = NodeCollector()
    result = await collector.collect()

    assert "scw-node-minimal" in result
    assert result["scw-node-minimal"].cloud_provider == "scaleway"
    assert result["scw-node-minimal"].instance_type == "GP1-XS"
