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


@pytest.fixture
def mock_processor():
    """Create a DataProcessor with all collectors mocked."""
    prom = AsyncMock()
    opencost = AsyncMock()
    node_col = AsyncMock()
    pod_col = AsyncMock()
    emaps = AsyncMock()
    boavizta = AsyncMock()
    repo = AsyncMock()
    node_repo = AsyncMock()
    embodied_repo = AsyncMock()
    calculator = AsyncMock(spec=CarbonCalculator)
    estimator = MagicMock(spec=BasicEstimator)

    processor = DataProcessor(
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
    )
    return processor


class TestProcessorResourceWiring:
    """Tests that the processor correctly maps new resource data to CombinedMetric."""

    @pytest.mark.asyncio
    async def test_network_io_populated_in_combined_metric(self, mock_processor):
        """Network I/O data from Prometheus should flow into CombinedMetric."""
        # Setup Prometheus to return network data
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

        mock_processor.prometheus_collector.collect = AsyncMock(return_value=prom_metrics)
        mock_processor.opencost_collector.collect = AsyncMock(return_value=[])
        mock_processor.node_collector.collect = AsyncMock(
            return_value={
                "n1": NodeInfo(
                    name="n1", cloud_provider="aws", instance_type="m5.large", zone="us-east-1a", region="us-east-1"
                ),
            }
        )
        mock_processor.node_collector.collect_instance_types = AsyncMock(return_value={"n1": "m5.large"})
        mock_processor.pod_collector.collect = AsyncMock(
            return_value=[
                PodMetric(
                    pod_name="p1", namespace="ns", container_name="c1", cpu_request=500, memory_request=268435456
                ),
            ]
        )

        # Setup estimator
        mock_processor.estimator.estimate = MagicMock(
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
        mock_processor.estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 3, "maxWatts": 36}}
        mock_processor.estimator.query_range_step_sec = 300

        # Setup calculator
        mock_processor.calculator.calculate_emissions = AsyncMock(
            return_value=CarbonCalculationResult(co2e_grams=10.0, grid_intensity=100.0)
        )
        mock_processor.calculator.clear_cache = AsyncMock()
        mock_processor.calculator._intensity_cache = {}
        mock_processor.calculator._lock = AsyncMock()

        # Setup repositories
        mock_processor.repository.get_for_zone_at_time = AsyncMock(return_value=100.0)
        mock_processor.embodied_repository.get_profile = AsyncMock(return_value=None)

        result = await mock_processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.network_receive_bytes == 1024000
        assert combined.network_transmit_bytes == 512000

    @pytest.mark.asyncio
    async def test_disk_io_populated_in_combined_metric(self, mock_processor):
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

        mock_processor.prometheus_collector.collect = AsyncMock(return_value=prom_metrics)
        mock_processor.opencost_collector.collect = AsyncMock(return_value=[])
        mock_processor.node_collector.collect = AsyncMock(
            return_value={
                "n1": NodeInfo(
                    name="n1", cloud_provider="aws", instance_type="m5.large", zone="us-east-1a", region="us-east-1"
                ),
            }
        )
        mock_processor.node_collector.collect_instance_types = AsyncMock(return_value={"n1": "m5.large"})
        mock_processor.pod_collector.collect = AsyncMock(
            return_value=[
                PodMetric(
                    pod_name="p1", namespace="ns", container_name="c1", cpu_request=500, memory_request=268435456
                ),
            ]
        )

        mock_processor.estimator.estimate = MagicMock(
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
        mock_processor.estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 3, "maxWatts": 36}}
        mock_processor.estimator.query_range_step_sec = 300

        mock_processor.calculator.calculate_emissions = AsyncMock(
            return_value=CarbonCalculationResult(co2e_grams=10.0, grid_intensity=100.0)
        )
        mock_processor.calculator.clear_cache = AsyncMock()
        mock_processor.calculator._intensity_cache = {}
        mock_processor.calculator._lock = AsyncMock()

        mock_processor.repository.get_for_zone_at_time = AsyncMock(return_value=100.0)
        mock_processor.embodied_repository.get_profile = AsyncMock(return_value=None)

        result = await mock_processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.disk_read_bytes == 2048000
        assert combined.disk_write_bytes == 1024000

    @pytest.mark.asyncio
    async def test_restart_count_populated_in_combined_metric(self, mock_processor):
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

        mock_processor.prometheus_collector.collect = AsyncMock(return_value=prom_metrics)
        mock_processor.opencost_collector.collect = AsyncMock(return_value=[])
        mock_processor.node_collector.collect = AsyncMock(
            return_value={
                "n1": NodeInfo(
                    name="n1", cloud_provider="aws", instance_type="m5.large", zone="us-east-1a", region="us-east-1"
                ),
            }
        )
        mock_processor.node_collector.collect_instance_types = AsyncMock(return_value={"n1": "m5.large"})
        mock_processor.pod_collector.collect = AsyncMock(
            return_value=[
                PodMetric(
                    pod_name="p1", namespace="ns", container_name="c1", cpu_request=500, memory_request=268435456
                ),
            ]
        )

        mock_processor.estimator.estimate = MagicMock(
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
        mock_processor.estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 3, "maxWatts": 36}}
        mock_processor.estimator.query_range_step_sec = 300

        mock_processor.calculator.calculate_emissions = AsyncMock(
            return_value=CarbonCalculationResult(co2e_grams=10.0, grid_intensity=100.0)
        )
        mock_processor.calculator.clear_cache = AsyncMock()
        mock_processor.calculator._intensity_cache = {}
        mock_processor.calculator._lock = AsyncMock()

        mock_processor.repository.get_for_zone_at_time = AsyncMock(return_value=100.0)
        mock_processor.embodied_repository.get_profile = AsyncMock(return_value=None)

        result = await mock_processor.run()

        assert len(result) >= 1
        combined = result[0]
        assert combined.restart_count == 3
