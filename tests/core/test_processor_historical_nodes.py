from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import EnergyMetric
from greenkube.models.node import NodeInfo


@pytest.mark.asyncio
async def test_run_range_uses_old_snapshot():
    """
    Verifies that run_range uses historical snapshots even if they are old.
    """
    # Arrange
    mock_repo = MagicMock()
    mock_repo.read_combined_metrics = AsyncMock(return_value=[])
    mock_node_repo = MagicMock()

    # Setup an old snapshot
    old_ts = datetime.now(timezone.utc) - timedelta(days=365)
    node_info = NodeInfo(
        name="node-1",
        instance_type="m5.large",
        zone="us-east-1a",
        region="us-east-1",
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2,
        memory_capacity_bytes=8589934592,
        timestamp=old_ts,
    )

    # Mock get_latest_snapshots_before to return the old snapshot
    mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[node_info])
    mock_node_repo.get_snapshots = AsyncMock(return_value=[])  # No changes during the interval

    # Mock Prometheus collector to return usage for a pod on this node
    mock_prom = MagicMock()
    mock_prom.collect_range = AsyncMock(
        return_value=[
            {
                "metric": {"namespace": "default", "pod": "test-pod", "node": "node-1"},
                "values": [(datetime.now(timezone.utc).timestamp(), "0.5")],
            }
        ]
    )

    # Mock estimator to return a profile
    mock_estimator = MagicMock()
    mock_estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 10, "maxWatts": 100}}
    mock_estimator.calculate_node_energy.return_value = [
        EnergyMetric(
            pod_name="test-pod",
            namespace="default",
            joules=100,
            node="node-1",
            timestamp=datetime.now(timezone.utc),
        )
    ]

    # Mock calculator
    mock_calculator = MagicMock()
    mock_calculator.calculate_emissions = AsyncMock(
        return_value=MagicMock(co2e_grams=50, grid_intensity=500, grid_intensity_timestamp=datetime.now(timezone.utc))
    )
    mock_calculator.pue = 1.2
    mock_calculator._intensity_cache = {}

    mock_node_collector = MagicMock()
    mock_node_collector.collect = AsyncMock(return_value={})
    mock_node_collector.collect_instance_types = AsyncMock(return_value={"node-1": "current-type"})

    processor = DataProcessor(
        prometheus_collector=mock_prom,
        opencost_collector=MagicMock(collect=AsyncMock()),
        node_collector=mock_node_collector,
        pod_collector=MagicMock(collect=AsyncMock()),
        electricity_maps_collector=MagicMock(collect=AsyncMock()),
        repository=mock_repo,
        node_repository=mock_node_repo,
        calculator=mock_calculator,
        estimator=mock_estimator,
    )

    start = datetime.now(timezone.utc) - timedelta(hours=1)
    end = datetime.now(timezone.utc)

    # Act
    metrics = await processor.run_range(start, end)

    # Assert
    assert len(metrics) == 1
    # The metric should have the instance type from the CURRENT collector, not the old snapshot
    assert metrics[0].node_instance_type == "current-type"

    # Verify that get_latest_snapshots_before was called
    mock_node_repo.get_latest_snapshots_before.assert_called()
