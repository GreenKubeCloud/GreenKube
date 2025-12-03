# tests/cli/test_start_analyze_nodes.py

from unittest.mock import patch

from greenkube.cli.start import analyze_nodes
from greenkube.models.node import NodeInfo


def test_analyze_nodes_success():
    """Test that analyze_nodes collects nodes and saves them to the repository."""

    # Mock NodeCollector
    with patch("greenkube.cli.start.NodeCollector") as MockNodeCollector:
        mock_collector = MockNodeCollector.return_value
        mock_collector.collect.return_value = {
            "node-1": NodeInfo(name="node-1", cloud_provider="aws", instance_type="m5.large")
        }

        # Mock get_node_repository
        with patch("greenkube.cli.start.get_node_repository") as mock_get_repo:
            mock_repo = mock_get_repo.return_value
            mock_repo.save_nodes.return_value = 1

            # Act
            analyze_nodes()

            # Assert
            mock_collector.collect.assert_called_once()
            mock_repo.save_nodes.assert_called_once()
            args, _ = mock_repo.save_nodes.call_args
            assert len(args[0]) == 1
            assert args[0][0].name == "node-1"


def test_analyze_nodes_no_nodes():
    """Test that analyze_nodes handles empty node list gracefully."""

    with patch("greenkube.cli.start.NodeCollector") as MockNodeCollector:
        mock_collector = MockNodeCollector.return_value
        mock_collector.collect.return_value = {}

        with patch("greenkube.cli.start.get_node_repository") as mock_get_repo:
            mock_repo = mock_get_repo.return_value

            # Act
            analyze_nodes()

            # Assert
            mock_collector.collect.assert_called_once()
            mock_repo.save_nodes.assert_not_called()


def test_analyze_nodes_exception():
    """Test that analyze_nodes handles exceptions gracefully."""

    with patch("greenkube.cli.start.NodeCollector") as MockNodeCollector:
        mock_collector = MockNodeCollector.return_value
        mock_collector.collect.side_effect = Exception("API Error")

        # Act
        # Should not raise exception
        analyze_nodes()
