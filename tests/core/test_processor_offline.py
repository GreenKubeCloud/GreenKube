from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import EnergyMetric
from greenkube.models.node import NodeInfo


# Self-contained fixtures for this test module
@pytest.fixture
def mock_prometheus_collector():
    return MagicMock()


@pytest.fixture
def mock_opencost_collector():
    return MagicMock()


@pytest.fixture
def mock_node_collector():
    return MagicMock()


@pytest.fixture
def mock_pod_collector():
    return MagicMock()


@pytest.fixture
def mock_electricity_maps_collector():
    return MagicMock()


@pytest.fixture
def mock_repository():
    return MagicMock()


@pytest.fixture
def mock_node_repository():
    return MagicMock()


@pytest.fixture
def mock_calculator():
    return MagicMock()


@pytest.fixture
def mock_basic_estimator():
    return MagicMock()


@pytest.mark.asyncio
async def test_processor_uses_cache_when_boavizta_offline(
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_pod_collector,
    mock_electricity_maps_collector,
    mock_repository,
    mock_node_repository,
    mock_calculator,
    mock_basic_estimator,
):
    """
    Verify that if Boavizta API fails, the processor uses the cached profile from EmbodiedRepository.
    """

    # 1. Setup Mocks

    # Boavizta Collector fails (Offline)
    mock_boavizta_collector = MagicMock()
    mock_boavizta_collector.get_server_impact = AsyncMock(side_effect=Exception("API Unreachable"))

    # Embodied Repository has cache (Hit)
    mock_embodied_repository = MagicMock()
    mock_embodied_repository.get_profile = AsyncMock(return_value={"gwp_manufacture": 1000.0, "lifespan_hours": 20000})
    mock_embodied_repository.save_profile = AsyncMock()

    # Node Collector returns a node with provider/instance
    mock_node_collector.collect = AsyncMock(
        return_value={
            "node-1": NodeInfo(name="node-1", cloud_provider="aws", instance_type="m5.large", zone="us-east-1a")
        }
    )
    # Also need this for fetch_nodes fallback or flow
    mock_node_collector.collect_instance_types = AsyncMock(return_value={"node-1": "m5.large"})

    # Calculator handles embodied calculation
    mock_calculator.calculate_embodied_emissions.return_value = 50.0
    mock_calculator.calculate_emissions = AsyncMock(
        return_value=MagicMock(co2e_grams=10, grid_intensity=10, grid_intensity_timestamp=None)
    )

    # Estimator returns energy metric
    mock_basic_estimator.estimate.return_value = [
        EnergyMetric(pod_name="pod-1", namespace="default", joules=100.0, node="node-1")
    ]

    # Prometheus Collector
    mock_prometheus_collector.collect = AsyncMock(return_value=MagicMock(node_instance_types=[], pod_cpu_usage=[]))

    # Opencost returns nothing (simplify)
    mock_opencost_collector.collect = AsyncMock(return_value=[])

    # Pod Collector returns requests
    from greenkube.models.metrics import PodMetric

    mock_pod_collector.collect = AsyncMock(
        return_value=[
            PodMetric(pod_name="pod-1", namespace="default", container_name="c1", cpu_request=500, memory_request=1024)
        ]
    )

    # Electricity Maps
    mock_electricity_maps_collector.collect = AsyncMock(return_value=[])

    # Repositories
    mock_repository.get_for_zone_at_time = AsyncMock(return_value=None)
    mock_repository.save_history = AsyncMock()
    mock_node_repository.get_latest_snapshots_before = AsyncMock(return_value=[])
    mock_node_repository.get_snapshots = AsyncMock(return_value=[])
    mock_node_repository.save_nodes = AsyncMock()

    # Mock estimator instance profiles for share calculation
    mock_basic_estimator.instance_profiles = {"m5.large": {"vcores": 2}}
    mock_basic_estimator.query_range_step_sec = 60

    dp = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_maps_collector=mock_electricity_maps_collector,
        boavizta_collector=mock_boavizta_collector,
        repository=mock_repository,
        node_repository=mock_node_repository,
        embodied_repository=mock_embodied_repository,
        calculator=mock_calculator,
        estimator=mock_basic_estimator,
    )

    # 2. Execute
    metrics = await dp.run()

    # 3. Verify
    assert len(metrics) == 1
    metric = metrics[0]

    # Embodied emissions should be 50.0 (from calculator mock) indicating the flow worked
    assert metric.embodied_co2e_grams == 50.0

    # Verify cached profile was used (API not called is implicit if we assume cache logic works,
    # but strictly if cache hits, get_server_impact shouldn't be called if logic is strictly short-circuiting.
    # Let's check logic: processor calls get_profile() first. If returns result, adds to cache.
    # Checks boavizta_cache. If missing, calls API.
    # So yes, API should NOT be called.
    mock_boavizta_collector.get_server_impact.assert_not_called()

    # Verify repository get_profile was called
    mock_embodied_repository.get_profile.assert_called_with("aws", "m5.large")


@pytest.mark.asyncio
async def test_processor_fallback_gracefully_when_both_fail(
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_pod_collector,
    mock_electricity_maps_collector,
    mock_repository,
    mock_node_repository,
    mock_calculator,
    mock_basic_estimator,
):
    """
    Verify that if Boavizta API fails AND cache misses, we proceed without crashing, with 0 embodied emissions.
    """

    # 1. Setup Mocks

    # Boavizta Collector fails (Offline)
    mock_boavizta_collector = MagicMock()
    mock_boavizta_collector.get_server_impact = AsyncMock(side_effect=Exception("API Unreachable"))

    # Embodied Repository Miss
    mock_embodied_repository = MagicMock()
    mock_embodied_repository.get_profile = AsyncMock(return_value=None)
    mock_embodied_repository.save_profile = AsyncMock()

    # Node Collector returns a node with provider/instance
    mock_node_collector.collect = AsyncMock(
        return_value={
            "node-1": NodeInfo(name="node-1", cloud_provider="aws", instance_type="m5.large", zone="us-east-1a")
        }
    )
    mock_node_collector.collect_instance_types = AsyncMock(return_value={"node-1": "m5.large"})

    # Calculator handles regular emissions
    mock_calculator.calculate_emissions = AsyncMock(
        return_value=MagicMock(co2e_grams=10, grid_intensity=10, grid_intensity_timestamp=None)
    )

    # Estimator returns energy metric
    mock_basic_estimator.estimate.return_value = [
        EnergyMetric(pod_name="pod-1", namespace="default", joules=100.0, node="node-1")
    ]

    mock_prometheus_collector.collect = AsyncMock(return_value=MagicMock(node_instance_types=[], pod_cpu_usage=[]))
    mock_opencost_collector.collect = AsyncMock(return_value=[])

    from greenkube.models.metrics import PodMetric

    mock_pod_collector.collect = AsyncMock(
        return_value=[
            PodMetric(pod_name="pod-1", namespace="default", container_name="c1", cpu_request=500, memory_request=1024)
        ]
    )

    mock_electricity_maps_collector.collect = AsyncMock(return_value=[])
    mock_repository.get_for_zone_at_time = AsyncMock(return_value=None)
    mock_repository.save_history = AsyncMock()
    mock_node_repository.get_latest_snapshots_before = AsyncMock(return_value=[])
    mock_node_repository.get_snapshots = AsyncMock(return_value=[])
    mock_node_repository.save_nodes = AsyncMock()

    mock_basic_estimator.instance_profiles = {"m5.large": {"vcores": 2}}
    mock_basic_estimator.query_range_step_sec = 60

    dp = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_maps_collector=mock_electricity_maps_collector,
        boavizta_collector=mock_boavizta_collector,
        repository=mock_repository,
        node_repository=mock_node_repository,
        embodied_repository=mock_embodied_repository,
        calculator=mock_calculator,
        estimator=mock_basic_estimator,
    )

    # 2. Execute
    metrics = await dp.run()

    # 3. Verify
    assert len(metrics) == 1
    metric = metrics[0]

    # Embodied emissions should be 0.0 because profile wasn't found
    assert metric.embodied_co2e_grams == 0.0

    # Verify API WAS called (because cache missed)
    mock_boavizta_collector.get_server_impact.assert_called()
