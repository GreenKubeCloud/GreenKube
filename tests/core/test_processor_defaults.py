from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.config import config
from greenkube.core.processor import DataProcessor
from greenkube.models.node import NodeInfo


@pytest.mark.asyncio
async def test_pue_and_zone_fallback():
    # Arrange
    mock_repo = MagicMock()
    mock_node_repo = AsyncMock()
    mock_prom = MagicMock()
    mock_estimator = MagicMock()
    mock_calculator = MagicMock()
    mock_node_collector = MagicMock()

    # Setup mocks
    mock_node_repo.get_latest_snapshots_before.return_value = []
    mock_node_repo.get_snapshots.return_value = []
    mock_prom.collect_range.return_value = []

    # Mock node collector to return a node with a specific provider and region
    # but NO zone mapping (to test region fallback)
    node_info = NodeInfo(
        name="node-1",
        instance_type="m5.large",
        zone="unknown-zone",
        region="us-east-1",  # Should map to US-VA or similar
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2,
        memory_capacity_bytes=8589934592,
        timestamp=datetime.now(timezone.utc),
    )
    mock_node_collector.collect.return_value = {"node-1": node_info}
    mock_node_collector.collect_instance_types.return_value = {"node-1": "m5.large"}

    processor = DataProcessor(
        prometheus_collector=mock_prom,
        opencost_collector=MagicMock(),
        node_collector=mock_node_collector,
        pod_collector=MagicMock(),
        electricity_maps_collector=MagicMock(),
        repository=mock_repo,
        node_repository=mock_node_repo,
        embodied_repository=AsyncMock(),
        boavizta_collector=AsyncMock(),
        calculator=mock_calculator,
        estimator=mock_estimator,
    )

    # Act - Test Zone Mapping Logic directly via private method
    node_contexts = await processor._get_node_emaps_map({"node-1": node_info})

    # Assert - Zone Mapping
    # us-east-1 should map to US-MISO-RTO or similar depending on mapping file,
    # but definitely NOT DEFAULT_ZONE if mapping works.
    # Let's check if it's NOT the default zone (assuming default is FR)
    assert node_contexts["node-1"].emaps_zone != config.DEFAULT_ZONE

    # Act - Test PUE Logic
    # We can check config.get_pue_for_provider directly
    aws_pue = config.get_pue_for_provider("aws")
    default_pue = config.DEFAULT_PUE

    # Assert - PUE
    # AWS PUE should be different from default if configured correctly in profiles
    # or at least return a float
    assert isinstance(aws_pue, float)
    assert config.get_pue_for_provider("unknown-provider") == default_pue
