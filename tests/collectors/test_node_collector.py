# tests/collectors/test_node_collector.py
from unittest.mock import patch
from src.greenkube.collectors.node_collector import NodeCollector
from kubernetes.config.config_exception import ConfigException
from unittest.mock import MagicMock, ANY

@patch('src.greenkube.collectors.node_collector.client')
@patch('src.greenkube.collectors.node_collector.config')
def test_collect_returns_zones(mock_k8s_config, mock_k8s_client):
    """
    Teste que la méthode collect retourne bien une liste de zones.
    """
    # Arrange
    # On configure le mock pour qu'il se comporte comme la vraie librairie
    mock_k8s_config.ConfigException = ConfigException
    mock_k8s_config.load_incluster_config.side_effect = ConfigException("Not in cluster")
    
    # On simule une réponse de l'API K8s
    mock_node = MagicMock()
    mock_node.metadata.labels = {"topology.kubernetes.io/zone": "europe-west9-a"}
    mock_k8s_client.CoreV1Api.return_value.list_node.return_value.items = [mock_node]

    collector = NodeCollector()

    # Act
    # On appelle la méthode avec le bon nom
    zones = collector.collect()

    # Assert
    assert isinstance(zones, list)
    assert "europe-west9-a" in zones

