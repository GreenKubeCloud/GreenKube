from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.historical_range_processor import HistoricalRangeProcessor, RangeCollectionError
from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import CombinedMetric


def _stored_metric() -> CombinedMetric:
    return CombinedMetric(
        pod_name="test-pod",
        namespace="default",
        timestamp=datetime(2023, 10, 23, 12, 0, tzinfo=timezone.utc),
        total_cost=0,
        co2e_grams=0,
        pue=1,
        grid_intensity=0,
        joules=0,
        cpu_request=0,
        memory_request=0,
    )


@pytest.mark.asyncio
async def test_run_range_never_short_circuits_on_stored_rows():
    """BUG-03: a stored row in the range must not make run_range skip the collection."""
    mock_repo = MagicMock()
    mock_repo.read_combined_metrics = AsyncMock(return_value=[_stored_metric()])
    prometheus = MagicMock()
    prometheus.collect_range = AsyncMock(return_value=[])
    prometheus.collect_pod_storage = AsyncMock(return_value={})

    node_collector = MagicMock(collect=AsyncMock(return_value={}), collect_instance_types=AsyncMock(return_value={}))
    node_collector.close = AsyncMock()

    processor = DataProcessor(
        prometheus_collector=prometheus,
        opencost_collector=AsyncMock(),
        node_collector=node_collector,
        pod_collector=MagicMock(collect=AsyncMock(return_value=[])),
        electricity_provider=AsyncMock(),
        repository=AsyncMock(),
        combined_metrics_repository=mock_repo,
        node_repository=AsyncMock(),
        embodied_repository=AsyncMock(),
        boavizta_collector=AsyncMock(),
        calculator=AsyncMock(),
        estimator=MagicMock(),
    )

    start = datetime(2023, 10, 23, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 10, 24, 0, 0, 0, tzinfo=timezone.utc)

    metrics = await processor.run_range(start, end)

    mock_repo.read_combined_metrics.assert_not_called()
    assert prometheus.collect_range.await_count > 0
    assert metrics == []


@pytest.mark.asyncio
async def test_run_range_raises_when_prometheus_is_unreachable():
    """An outage must fail the window instead of being stored as an empty window."""
    prometheus = MagicMock()
    prometheus.collect_range = AsyncMock(side_effect=RuntimeError("connection refused"))
    prometheus.collect_pod_storage = AsyncMock(return_value={})
    node_repository = MagicMock()
    node_repository.get_latest_snapshots_before = AsyncMock(return_value=[])
    node_repository.get_snapshots = AsyncMock(return_value=[])
    zone_mapper = MagicMock()
    zone_mapper.map_nodes = AsyncMock(return_value={})
    assembler = MagicMock()
    assembler.embodied_service.prepare_embodied_data = AsyncMock(return_value={})
    processor = HistoricalRangeProcessor(
        prometheus_collector=prometheus,
        opencost_collector=MagicMock(collect_range=AsyncMock(return_value=[])),
        node_collector=MagicMock(collect=AsyncMock(return_value={}), collect_instance_types=AsyncMock(return_value={})),
        pod_collector=MagicMock(collect=AsyncMock(return_value=[])),
        repository=MagicMock(),
        combined_metrics_repository=MagicMock(),
        node_repository=node_repository,
        calculator=MagicMock(clear_cache=AsyncMock()),
        estimator=MagicMock(),
        assembler=assembler,
        zone_mapper=zone_mapper,
    )

    with pytest.raises(RangeCollectionError):
        await processor.run_range(
            datetime(2023, 10, 23, 10, 0, tzinfo=timezone.utc),
            datetime(2023, 10, 23, 10, 5, tzinfo=timezone.utc),
        )
