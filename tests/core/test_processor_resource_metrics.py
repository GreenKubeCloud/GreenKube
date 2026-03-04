# tests/core/test_processor_resource_metrics.py
"""
Tests for the DataProcessor wiring of extended resource metrics.

Validates that the processor correctly populates CombinedMetric with:
- Network I/O from Prometheus
- Disk I/O from Prometheus
- Storage requests from PodCollector
- Restart counts from Prometheus
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculationResult, CarbonCalculator
from greenkube.core.processor import DataProcessor
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.metrics import EnergyMetric, PodMetric
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import (
    PodCPUUsage,
    PodDiskIO,
    PodMemoryUsage,
    PodNetworkIO,
    PodRestartCount,
    PrometheusMetric,
)


def _build_processor(prom_metrics, pod_metrics, node_info_map):
    """Helper to build a DataProcessor with pre-configured mocks."""
    prom = AsyncMock()
    prom.collect = AsyncMock(return_value=prom_metrics)
    prom.collect_range = AsyncMock(return_value=[])

    opencost = AsyncMock()
    opencost.collect = AsyncMock(return_value=[])

    node_col = AsyncMock()
    node_col.collect = AsyncMock(return_value=node_info_map)
    node_col.collect_instance_types = AsyncMock(return_value={k: v.instance_type for k, v in node_info_map.items()})

    pod_col = AsyncMock()
    pod_col.collect = AsyncMock(return_value=pod_metrics)

    emaps = AsyncMock()
    emaps.collect = AsyncMock(return_value=[])

    boavizta = AsyncMock()
    boavizta.get_server_impact = AsyncMock(return_value=None)

    repo = AsyncMock()
    repo.get_for_zone_at_time = AsyncMock(return_value=100.0)
    repo.save_history = AsyncMock()
    repo.read_combined_metrics = AsyncMock(return_value=[])

    node_repo = AsyncMock()
    node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    node_repo.get_snapshots = AsyncMock(return_value=[])
    node_repo.save_nodes = AsyncMock()

    embodied_repo = AsyncMock()
    embodied_repo.get_profile = AsyncMock(return_value=None)
    embodied_repo.save_profile = AsyncMock()

    calculator = AsyncMock(spec=CarbonCalculator)
    calculator.calculate_emissions = AsyncMock(
        return_value=CarbonCalculationResult(co2e_grams=10.0, grid_intensity=100.0)
    )
    calculator.clear_cache = AsyncMock()
    calculator._intensity_cache = {}
    calculator._lock = AsyncMock()

    estimator = MagicMock(spec=BasicEstimator)
    estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 3, "maxWatts": 36}}
    estimator.query_range_step_sec = 300

    return DataProcessor(
        prometheus_collector=prom,
        opencost_collector=opencost,
        node_collector=node_col,
        pod_collector=pod_col,
        electricity_maps_collector=emaps,
        boavizta_collector=boavizta,
        repository=repo,
        node_repository=node_repo,
        embodied_repository=embodied_repo,
        calculator=calculator,
        estimator=estimator,
    ), estimator


NODE_INFO = {
    "n1": NodeInfo(
        name="n1",
        cloud_provider="aws",
        instance_type="m5.large",
        zone="us-east-1a",
        region="us-east-1",
    ),
}

POD_METRICS = [
    PodMetric(
        pod_name="p1",
        namespace="ns",
        container_name="c1",
        cpu_request=500,
        memory_request=268435456,
    ),
]


class TestProcessorResourceWiring:
    """Tests that the processor correctly maps new resource data to CombinedMetric."""

    @pytest.mark.asyncio
    async def test_network_io_populated_in_combined_metric(self):
        """Network I/O data from Prometheus should flow into CombinedMetric."""
        prom_metrics = PrometheusMetric(
            pod_cpu_usage=[
                PodCPUUsage(namespace="ns", pod="p1", container="c1", node="n1", cpu_usage_cores=0.5),
            ],
            pod_memory_usage=[
                PodMemoryUsage(namespace="ns", pod="p1", node="n1", memory_usage_bytes=104857600),
            ],
            pod_network_io=[
                PodNetworkIO(
                    namespace="ns", pod="p1", node="n1", network_receive_bytes=1024000, network_transmit_bytes=512000
                ),
            ],
            pod_disk_io=[],
            pod_restart_counts=[],
        )

        processor, estimator = _build_processor(prom_metrics, POD_METRICS, NODE_INFO)
        estimator.estimate = MagicMock(
            return_value=[
                EnergyMetric(
                    pod_name="p1",
                    namespace="ns",
                    joules=50000.0,
                    node="n1",
                    timestamp=datetime(2025, 2, 20, 12, 0, tzinfo=timezone.utc),
                ),
            ]
        )

        result = await processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.network_receive_bytes == 1024000
        assert combined.network_transmit_bytes == 512000

    @pytest.mark.asyncio
    async def test_disk_io_populated_in_combined_metric(self):
        """Disk I/O data from Prometheus should flow into CombinedMetric."""
        prom_metrics = PrometheusMetric(
            pod_cpu_usage=[
                PodCPUUsage(namespace="ns", pod="p1", container="c1", node="n1", cpu_usage_cores=0.5),
            ],
            pod_memory_usage=[
                PodMemoryUsage(namespace="ns", pod="p1", node="n1", memory_usage_bytes=104857600),
            ],
            pod_network_io=[],
            pod_disk_io=[
                PodDiskIO(namespace="ns", pod="p1", node="n1", disk_read_bytes=2048000, disk_write_bytes=1024000),
            ],
            pod_restart_counts=[],
        )

        processor, estimator = _build_processor(prom_metrics, POD_METRICS, NODE_INFO)
        estimator.estimate = MagicMock(
            return_value=[
                EnergyMetric(
                    pod_name="p1",
                    namespace="ns",
                    joules=50000.0,
                    node="n1",
                    timestamp=datetime(2025, 2, 20, 12, 0, tzinfo=timezone.utc),
                ),
            ]
        )

        result = await processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.disk_read_bytes == 2048000
        assert combined.disk_write_bytes == 1024000

    @pytest.mark.asyncio
    async def test_restart_count_populated_in_combined_metric(self):
        """Restart count from Prometheus should flow into CombinedMetric."""
        prom_metrics = PrometheusMetric(
            pod_cpu_usage=[
                PodCPUUsage(namespace="ns", pod="p1", container="c1", node="n1", cpu_usage_cores=0.5),
            ],
            pod_memory_usage=[
                PodMemoryUsage(namespace="ns", pod="p1", node="n1", memory_usage_bytes=104857600),
            ],
            pod_network_io=[],
            pod_disk_io=[],
            pod_restart_counts=[
                PodRestartCount(namespace="ns", pod="p1", container="c1", restart_count=3),
            ],
        )

        processor, estimator = _build_processor(prom_metrics, POD_METRICS, NODE_INFO)
        estimator.estimate = MagicMock(
            return_value=[
                EnergyMetric(
                    pod_name="p1",
                    namespace="ns",
                    joules=50000.0,
                    node="n1",
                    timestamp=datetime(2025, 2, 20, 12, 0, tzinfo=timezone.utc),
                ),
            ]
        )

        result = await processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.restart_count == 3
