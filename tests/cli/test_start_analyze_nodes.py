# tests/cli/test_start_analyze_nodes.py

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.cli.start import analyze_nodes
from greenkube.models.node import NodeInfo


@pytest.mark.asyncio
async def test_analyze_nodes_success():
    """Test successful node analysis and saving."""
    with (
        patch("greenkube.cli.start.NodeCollector") as MockCollector,
        patch("greenkube.cli.start.get_node_repository") as mock_get_repo,
    ):
        # Setup mocks
        mock_collector = MockCollector.return_value
        # Mock collect as async
        mock_collector.collect = AsyncMock()
        mock_collector.close = AsyncMock()

        mock_repo = MagicMock()
        # Mock save_nodes as async
        mock_repo.save_nodes = AsyncMock(return_value=2)
        mock_get_repo.return_value = mock_repo

        mock_nodes = {
            "node1": NodeInfo(name="node1", zone="zone1", cloud_provider="aws", instance_type="t3.medium"),
            "node2": NodeInfo(name="node2", zone="zone1", cloud_provider="aws", instance_type="t3.large"),
        }
        mock_collector.collect.return_value = mock_nodes

        # Execute
        await analyze_nodes()

        # Verify
        mock_collector.collect.assert_called_once()
        mock_repo.save_nodes.assert_called_once()
        saved_nodes = mock_repo.save_nodes.call_args[0][0]
        assert len(saved_nodes) == 2


@pytest.mark.asyncio
async def test_analyze_nodes_no_nodes():
    """Test behavior when no nodes are found."""
    with (
        patch("greenkube.cli.start.NodeCollector") as MockCollector,
        patch("greenkube.cli.start.get_node_repository") as mock_get_repo,
    ):
        mock_collector = MockCollector.return_value
        mock_collector.collect = AsyncMock(return_value={})
        mock_collector.close = AsyncMock()

        mock_repo = MagicMock()
        mock_get_repo.return_value = mock_repo

        # Execute
        await analyze_nodes()

        # Verify
        mock_collector.collect.assert_called_once()
        mock_repo.save_nodes.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_nodes_exception():
    """Test error handling during analysis."""
    with (
        patch("greenkube.cli.start.NodeCollector") as MockCollector,
        patch("greenkube.cli.start.get_node_repository") as mock_get_repo,
    ):
        mock_collector = MockCollector.return_value
        mock_collector.collect = AsyncMock(side_effect=Exception("API Error"))
        mock_collector.close = AsyncMock()

        mock_repo = MagicMock()
        mock_get_repo.return_value = mock_repo

        # Execute (should not raise exception, but log error)
        await analyze_nodes()

        # Verify
        mock_collector.collect.assert_called_once()
        mock_repo.save_nodes.assert_not_called()
