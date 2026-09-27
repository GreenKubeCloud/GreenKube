from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import CombinedMetric


@pytest.mark.asyncio
async def test_run_range_uses_repository():
    # Arrange
    mock_repo = MagicMock()
    mock_repo.read_combined_metrics = AsyncMock(
        return_value=[
            CombinedMetric(
                pod_name="test-pod",
                namespace="default",
                timestamp=datetime.now(timezone.utc),
                total_cost=0,
                co2e_grams=0,
                pue=1,
                grid_intensity=0,
                joules=0,
                cpu_request=0,
                memory_request=0,
            )
        ]
    )

    # Use AsyncMock for collectors that have async close() methods
    processor = DataProcessor(
        prometheus_collector=AsyncMock(),
        opencost_collector=AsyncMock(),
        node_collector=AsyncMock(),
        pod_collector=AsyncMock(),
        electricity_provider=AsyncMock(),
        repository=AsyncMock(),
        combined_metrics_repository=mock_repo,
        node_repository=AsyncMock(),
        embodied_repository=AsyncMock(),
        boavizta_collector=AsyncMock(),
        calculator=MagicMock(),
        estimator=MagicMock(),
    )

    start = datetime(2023, 10, 23, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 10, 24, 0, 0, 0, tzinfo=timezone.utc)

    # Act
    metrics = await processor.run_range(start, end)

    # Assert
    mock_repo.read_combined_metrics.assert_called_once_with(start, end)
    assert len(metrics) == 1
    assert metrics[0].pod_name == "test-pod"


@pytest.mark.asyncio
async def test_read_stored_metrics_filters_namespace_before_short_circuit():
    """A stored row for another namespace must not mask the requested namespace."""
    from datetime import datetime as dt

    from greenkube.core.historical_range_processor import HistoricalRangeProcessor

    repo = MagicMock()
    repo.read_combined_metrics = AsyncMock(
        return_value=[
            CombinedMetric(
                pod_name="other-pod",
                namespace="other",
                timestamp=dt.now(timezone.utc),
                total_cost=0,
                co2e_grams=0,
                pue=1,
                grid_intensity=0,
                joules=0,
                cpu_request=0,
                memory_request=0,
            )
        ]
    )
    processor = HistoricalRangeProcessor(
        prometheus_collector=MagicMock(),
        opencost_collector=MagicMock(),
        node_collector=MagicMock(),
        pod_collector=MagicMock(),
        repository=MagicMock(),
        combined_metrics_repository=repo,
        node_repository=MagicMock(),
        calculator=MagicMock(),
        estimator=MagicMock(),
        assembler=MagicMock(),
        zone_mapper=MagicMock(),
    )

    stored = await processor._read_stored_metrics(dt.now(timezone.utc), dt.now(timezone.utc), namespace="wanted")

    assert stored == []
