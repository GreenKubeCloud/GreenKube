from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.core.processor import DataProcessor
from greenkube.models.metrics import CostMetric, EnergyMetric
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import NodeInstanceType, PodCPUUsage, PrometheusMetric


@pytest.fixture
def mock_components():
    # Use AsyncMock for all collectors to prevent coroutine warnings
    prom = AsyncMock()
    prom.collect = AsyncMock()
    prom.collect_range = AsyncMock()

    opencost = AsyncMock()
    opencost.collect = AsyncMock()

    node = AsyncMock()
    node.collect = AsyncMock()
    node.collect_instance_types = AsyncMock()

    pod = AsyncMock()
    pod.collect = AsyncMock()

    emaps = AsyncMock()
    emaps.collect = AsyncMock()

    repo = AsyncMock()
    repo.get_for_zone_at_time = AsyncMock()
    repo.save_history = AsyncMock()
    repo.read_combined_metrics = AsyncMock()

    node_repo = AsyncMock()
    node_repo.get_latest_snapshots_before = AsyncMock()
    node_repo.get_snapshots = AsyncMock()
    node_repo.save_nodes = AsyncMock()

    calc = AsyncMock()
    calc.calculate_emissions = AsyncMock()
    calc.clear_cache = AsyncMock()  # async method

    return {
        "prom": prom,
        "opencost": opencost,
        "node": node,
        "pod": pod,
        "emaps": emaps,
        "repo": repo,
        "node_repo": node_repo,
        "calc": calc,
        "est": MagicMock(),
    }


@pytest.fixture
def processor(mock_components):
    return DataProcessor(
        prometheus_collector=mock_components["prom"],
        opencost_collector=mock_components["opencost"],
        node_collector=mock_components["node"],
        pod_collector=mock_components["pod"],
        electricity_maps_collector=mock_components["emaps"],
        repository=mock_components["repo"],
        node_repository=mock_components["node_repo"],
        embodied_repository=AsyncMock(),
        boavizta_collector=AsyncMock(),
        calculator=mock_components["calc"],
        estimator=mock_components["est"],
    )


@pytest.mark.asyncio
async def test_run_full_defaults(processor, mock_components):
    # Arrange
    # 1. Prometheus returns data
    mock_components["prom"].collect.return_value = PrometheusMetric(
        pod_cpu_usage=[PodCPUUsage(namespace="ns", pod="pod-1", container="c1", node="node-1", cpu_usage_cores=0.1)],
        node_instance_types=[NodeInstanceType(node="node-1", instance_type="unknown-type")],
    )

    # 2. Estimator returns estimated metric (simulating unknown instance profile)
    # We mock the estimator to return a metric that has is_estimated=True
    mock_components["est"].estimate.return_value = [
        EnergyMetric(
            pod_name="pod-1",
            namespace="ns",
            joules=100.0,
            timestamp=datetime.now(timezone.utc),
            node="node-1",
            is_estimated=True,
            estimation_reasons=["Unknown instance type"],
        )
    ]

    # 3. Node Collector returns node info with unknown zone/provider
    # Actually, if we want to trigger zone fallback, we can make it return empty or a node with unknown zone
    mock_components["node"].collect.return_value = {
        "node-1": NodeInfo(
            name="node-1",
            zone="unknown-zone",
            region="unknown-region",
            cloud_provider="unknown-provider",
            instance_type="unknown-type",
            cpu_capacity_cores=2,
            memory_capacity_bytes=1024,
            timestamp=datetime.now(timezone.utc),
        )
    }

    # 4. OpenCost returns nothing (trigger cost default)
    mock_components["opencost"].collect.return_value = []

    # 5. Calculator returns a result
    mock_components["calc"].calculate_emissions.return_value = MagicMock(
        co2e_grams=50.0, grid_intensity=100.0, grid_intensity_timestamp=datetime.now(timezone.utc)
    )

    # Act
    metrics = await processor.run()

    # Assert
    assert len(metrics) == 1
    m = metrics[0]
    assert m.is_estimated is True
    # Check for reasons
    reasons_str = " ".join(m.estimation_reasons)
    assert "Unknown instance type" in reasons_str
    assert "default zone" in reasons_str or "Could not map" in reasons_str
    assert "No cost data" in reasons_str
    assert "Unknown provider" in reasons_str or "No PUE profile" in reasons_str


@pytest.mark.asyncio
async def test_run_no_defaults(processor, mock_components):
    # Arrange
    # 1. Prometheus
    mock_components["prom"].collect.return_value = PrometheusMetric(
        pod_cpu_usage=[PodCPUUsage(namespace="ns", pod="pod-1", container="c1", node="node-1", cpu_usage_cores=0.1)],
        node_instance_types=[NodeInstanceType(node="node-1", instance_type="m5.large")],
    )

    # 2. Estimator returns NOT estimated metric
    mock_components["est"].estimate.return_value = [
        EnergyMetric(
            pod_name="pod-1",
            namespace="ns",
            joules=100.0,
            timestamp=datetime.now(timezone.utc),
            node="node-1",
            is_estimated=False,
            estimation_reasons=[],
        )
    ]

    # 3. Node Collector returns valid zone/provider
    # We need to mock get_emaps_zone_from_cloud_zone or ensure it works.
    # Since we can't easily mock the standalone function imported in processor.py without patching,
    # we'll rely on the fact that "us-east-1a" usually maps to something if the map file is present.
    # Or better, we patch the method on the processor or the imported function.
    # For now, let's assume "us-east-1a" maps to "US-MISO-RTO" or similar if we are lucky,
    # BUT `get_emaps_zone_from_cloud_zone` might fail if files are missing in this environment.
    # So we should probably patch `greenkube.core.processor.get_emaps_zone_from_cloud_zone`.

    mock_components["node"].collect.return_value = {
        "node-1": NodeInfo(
            name="node-1",
            zone="us-east-1a",
            region="us-east-1",
            cloud_provider="aws",
            instance_type="m5.large",
            cpu_capacity_cores=2,
            memory_capacity_bytes=1024,
            timestamp=datetime.now(timezone.utc),
        )
    }

    # 4. OpenCost returns data
    mock_components["opencost"].collect.return_value = [
        CostMetric(
            pod_name="pod-1",
            namespace="ns",
            cpu_cost=0.1,
            ram_cost=0.1,
            total_cost=0.2,
            timestamp=datetime.now(timezone.utc),
        )
    ]

    # 5. Calculator
    mock_components["calc"].calculate_emissions.return_value = MagicMock(
        co2e_grams=50.0, grid_intensity=100.0, grid_intensity_timestamp=datetime.now(timezone.utc)
    )

    # Patch the zone mapper to ensure it returns a value
    with patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone", return_value="US-TEST"):
        # Act
        metrics = await processor.run()

    # Assert
    assert len(metrics) == 1
    m = metrics[0]
    assert m.is_estimated is False
    assert len(m.estimation_reasons) == 0
