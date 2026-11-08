# tests/core/test_processor.py

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.metrics_server_collector import MetricsServerCollector
from greenkube.core.calculator import CarbonCalculationResult
from greenkube.core.config import Config, config
from greenkube.core.processor import DataProcessor
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.metrics import (
    CostMetric,
    EnergyMetric,
    PodMetric,
)
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import ContainerResourceUsage, PrometheusMetric

# Sample data for mocking collectors
SAMPLE_ENERGY_METRICS = [
    EnergyMetric(
        pod_name="pod-A",
        namespace="ns-1",
        joules=1000000,
        timestamp="2023-10-27T10:00:00Z",
        node="node-1",
        region="us-east-1",
    ),
    EnergyMetric(
        pod_name="pod-B",
        namespace="ns-2",
        joules=2000000,
        timestamp="2023-10-27T10:05:00Z",
        node="node-2",
        region="eu-west-1",
    ),
    EnergyMetric(
        pod_name="pod-C",
        namespace="ns-1",
        joules=500000,
        timestamp="2023-10-27T10:02:00Z",
        node="node-1",
        region="us-east-1",
    ),  # Pod for missing cost
    EnergyMetric(
        pod_name="pod-D",
        namespace="ns-3",
        joules=300000,
        timestamp="2023-10-27T10:03:00Z",
        node="node-unknown",
        region="unknown",
    ),  # Pod for missing node zone
]

SAMPLE_COST_METRICS = [
    CostMetric(
        pod_name="pod-A",
        namespace="ns-1",
        cpu_cost=0.1,
        ram_cost=0.2,
        total_cost=0.3,
        timestamp="2023-10-27T10:00:00Z",
    ),
    CostMetric(
        pod_name="pod-B",
        namespace="ns-2",
        cpu_cost=0.4,
        ram_cost=0.5,
        total_cost=0.9,
        timestamp="2023-10-27T10:05:00Z",
    ),
    # No cost metric for pod-C intentionally
]

# Create NodeInfo objects for test data
SAMPLE_NODE_INFO = {
    "node-1": NodeInfo(
        name="node-1",
        zone="gcp-us-east1-a",
        region="us-east1",
        cloud_provider="gcp",
        instance_type="m5.large",
        architecture="amd64",
        node_pool=None,
    ),
    "node-2": NodeInfo(
        name="node-2",
        zone="aws-eu-west-1b",
        region="eu-west-1",
        cloud_provider="aws",
        instance_type="m5.large",
        architecture="amd64",
        node_pool=None,
    ),
    # No mapping for node-unknown intentionally
}

SAMPLE_CALCULATION_RESULT_A = CarbonCalculationResult(co2e_grams=50.0, grid_intensity=100.0)
SAMPLE_CALCULATION_RESULT_B = CarbonCalculationResult(co2e_grams=150.0, grid_intensity=120.0)
SAMPLE_CALCULATION_RESULT_C = CarbonCalculationResult(
    co2e_grams=25.0, grid_intensity=100.0
)  # Using same intensity as A for simplicity
SAMPLE_CALCULATION_RESULT_D = CarbonCalculationResult(
    co2e_grams=10.0, grid_intensity=config.DEFAULT_INTENSITY
)  # Uses default zone -> default intensity


# --- Fixtures for Mocks ---
@pytest.fixture
def mock_kepler_collector():
    """Provides a mock collector placeholder (was Kepler in older versions)."""
    mock = MagicMock()
    # Mock collect as async
    from unittest.mock import AsyncMock

    mock.collect = AsyncMock(return_value=SAMPLE_ENERGY_METRICS)
    return mock


@pytest.fixture
def mock_prometheus_collector():
    """Provides a mock PrometheusCollector that returns a PrometheusMetric placeholder."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    prom_metric_mock = MagicMock(spec=PrometheusMetric)
    prom_metric_mock.pod_cpu_usage = []
    prom_metric_mock.node_instance_types = []
    mock.collect = AsyncMock(return_value=prom_metric_mock)
    mock.collect_range = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_opencost_collector():
    """Provides a mock OpenCostCollector."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.collect = AsyncMock(return_value=SAMPLE_COST_METRICS)
    return mock


@pytest.fixture
def mock_node_collector():
    """Provides a mock NodeCollector that returns NodeInfo objects."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.collect = AsyncMock(return_value=SAMPLE_NODE_INFO)
    mock.collect_instance_types = AsyncMock(return_value={"node-1": "m5.large", "node-2": "m5.large"})
    return mock


@pytest.fixture
def mock_pod_collector():
    """Provides a mock PodCollector."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.collect = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_repository():
    """Provides a mock CarbonIntensityRepository."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.get_for_zone_at_time = AsyncMock(return_value=None)
    mock.save_history = AsyncMock()
    mock.read_combined_metrics = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_calculator():
    """Provides a mock CarbonCalculator."""
    mock = MagicMock()

    # Configure a side effect to return different results based on input
    # Configure a side effect to return different results based on input
    async def calculate_side_effect(joules, zone, timestamp, pue=None):
        # Determine pod name based on joules for simplified mapping in tests
        pod_name = None
        if joules == 1000000:
            pod_name = "pod-A"
        elif joules == 2000000:
            pod_name = "pod-B"
        elif joules == 500000:
            pod_name = "pod-C"
        elif joules == 300000:
            pod_name = "pod-D"

        if pod_name == "pod-A":
            return SAMPLE_CALCULATION_RESULT_A
        if pod_name == "pod-B":
            return SAMPLE_CALCULATION_RESULT_B
        if pod_name == "pod-C":
            return SAMPLE_CALCULATION_RESULT_C
        if pod_name == "pod-D":
            return SAMPLE_CALCULATION_RESULT_D
        print(f"WARN: Mock calculator received unexpected joules: {joules}")  # Added warning
        return CarbonCalculationResult(0.0, 0.0)  # Default fallback

    mock.calculate_emissions.side_effect = calculate_side_effect
    mock.pue = config.DEFAULT_PUE  # Set the pue attribute as the processor reads it
    mock._intensity_cache = {}  # Initialize the cache
    mock.clear_cache = AsyncMock()
    return mock


@pytest.fixture
def mock_electricity_maps_collector():
    """Provides a mock ElectricityMapsCollector."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.collect = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_boavizta_collector():
    """Provides a mock BoaviztaCollector."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.get_server_impact = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_node_repository():
    """Provides a mock NodeRepository."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.get_latest_snapshots_before = AsyncMock(return_value=[])
    mock.get_snapshots = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_embodied_repository():
    """Provides a mock EmbodiedRepository."""
    from unittest.mock import AsyncMock

    mock = MagicMock()
    mock.get_profile = AsyncMock(return_value=None)
    mock.save_profile = AsyncMock()
    return mock


@pytest.fixture
def data_processor(
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_pod_collector,
    mock_electricity_maps_collector,
    mock_boavizta_collector,
    mock_repository,
    mock_node_repository,
    mock_embodied_repository,
    mock_calculator,
):
    """
    Provides an instance of DataProcessor with mocked dependencies.
    Injects the new mock_repository and mock_calculator.
    """
    # Build a basic estimator mock that will convert Prometheus metrics to SAMPLE_ENERGY_METRICS
    estimator_mock = MagicMock(spec=BasicEstimator)
    estimator_mock.estimate.return_value = SAMPLE_ENERGY_METRICS
    # Also mock instance_profiles for run_range tests
    estimator_mock.instance_profiles = {}
    estimator_mock.DEFAULT_INSTANCE_PROFILE = {"vcores": 2, "minWatts": 10, "maxWatts": 50}
    estimator_mock.calculate_node_energy.return_value = []  # Default return
    estimator_mock.query_range_step_sec = 300  # 5 minutes

    return DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_provider=mock_electricity_maps_collector,
        boavizta_collector=mock_boavizta_collector,
        repository=mock_repository,  # Pass the mock repository
        combined_metrics_repository=MagicMock(),
        node_repository=mock_node_repository,
        embodied_repository=mock_embodied_repository,
        calculator=mock_calculator,  # Pass the mock calculator
        estimator=estimator_mock,
    )


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_run_range_uses_historical_node_data(
    mock_translator,
    data_processor,
    mock_prometheus_collector,
    mock_node_repository,
    mock_calculator,
):
    """
    Tests that run_range uses historical node data from NodeRepository.
    """
    from datetime import datetime, timezone

    # Arrange
    start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

    # Ensure repository doesn't return stored metrics (simulating fresh calculation)
    mock_repository = data_processor.repository
    mock_repository.read_combined_metrics.return_value = []

    # Mock translator to return a valid zone string
    mock_translator.return_value = "US-EAST-1"

    # Mock Prometheus range data
    mock_prometheus_collector.collect_range.return_value = [
        {
            "metric": {"namespace": "ns-1", "pod": "pod-A", "node": "node-1"},
            # Evaluated at 10:05, the sample describes [10:00, 10:05) and is stored at 10:00.
            "values": [[1672567500, "1.0"]],
        }
    ]

    # Mock NodeRepository snapshots
    # Initial state: node-1 is t3.medium (2 cores)
    mock_node_repository.get_latest_snapshots_before.return_value = [
        NodeInfo(
            name="node-1",
            instance_type="t3.medium",
            cpu_capacity_cores=2.0,
            zone="us-east-1a",
            region="us-east-1",
            cloud_provider="aws",
            architecture="amd64",
            node_pool="default",
        )
    ]
    # Change at 10:30: node-1 becomes t3.large (4 cores)
    mock_node_repository.get_snapshots.return_value = [
        (
            "2023-01-01T10:30:00+00:00",
            NodeInfo(
                name="node-1",
                instance_type="t3.large",
                cpu_capacity_cores=4.0,
                zone="us-east-1a",
                region="us-east-1",
                cloud_provider="aws",
                architecture="amd64",
                node_pool="default",
            ),
        )
    ]

    # Mock Estimator behavior
    # We need to verify that calculate_node_energy is called with different profiles
    # But since we mocked estimator in fixture, we need to inspect calls or side effects

    # Let's mock calculate_node_energy to return dummy metrics so run_range completes
    data_processor.estimator.calculate_node_energy.return_value = [
        EnergyMetric(pod_name="pod-A", namespace="ns-1", joules=100, node="node-1")
    ]

    # We also need to mock instance_profiles in estimator if we want profile lookup to work,
    # OR we can rely on the fact that processor calls profile_for_node which uses estimator.instance_profiles
    data_processor.estimator.instance_profiles = {
        "t3.medium": {"vcores": 2, "minWatts": 10, "maxWatts": 20},
        "t3.large": {"vcores": 4, "minWatts": 20, "maxWatts": 40},
    }

    # Act
    rows = await data_processor.run_range(start, end)

    # Assert
    # Verify that get_latest_snapshots_before and get_snapshots were called
    mock_node_repository.get_latest_snapshots_before.assert_called_once()
    mock_node_repository.get_snapshots.assert_called_once()

    # The first row describes [10:00, 10:05) (evaluated at 10:05), so the
    # Impact Model allocates the t3.medium profile of that time (2 cores,
    # 10-20 W): 1 core used -> 10 + 0.5 * 10 = 15 W, never the later t3.large
    # (25 W). Pod rows plus the node's __idle__ row add up to the node power.
    pod = next(row for row in rows if row.pod_name == "pod-A" and not row.container_name)
    (idle,) = data_processor._range_processor.last_idle_metrics
    assert idle.node == "node-1"
    assert pod.joules + idle.joules == pytest.approx(15 * 300, rel=1e-6)


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_run_range_preserves_container_metrics_for_the_scheduled_pipeline(
    mock_translator,
    data_processor,
    mock_prometheus_collector,
    mock_pod_collector,
    mock_node_repository,
):
    start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    mock_translator.return_value = "FR"
    data_processor._range_processor.combined_metrics_repository.read_combined_metrics = AsyncMock(return_value=[])
    data_processor._range_processor.opencost_collector.collect_range = AsyncMock(return_value=[])
    mock_prometheus_collector.collect_pod_storage = AsyncMock(return_value={})
    mock_pod_collector.collect = AsyncMock(
        return_value=[
            PodMetric(
                pod_name="pod-A",
                namespace="ns-1",
                container_name="web",
                cpu_request=500,
                memory_request=256 * 1024**2,
                owner_kind="Deployment",
                owner_name="api",
            ),
            PodMetric(
                pod_name="pod-A",
                namespace="ns-1",
                container_name="telemetry-sidecar",
                cpu_request=100,
                memory_request=64 * 1024**2,
                owner_kind="Deployment",
                owner_name="api",
            ),
        ]
    )

    async def collect_range(*, start, end, step=None, query=None, namespace=None):
        if query and "container_cpu_usage_seconds_total" in query:
            return [
                {
                    "metric": {"namespace": "ns-1", "pod": "pod-A", "container": "web", "node": "node-1"},
                    "values": [[1672567500, "0.02"]],
                },
                {
                    "metric": {
                        "namespace": "ns-1",
                        "pod": "pod-A",
                        "container": "telemetry-sidecar",
                        "node": "node-1",
                    },
                    "values": [[1672567500, "0.01"]],
                },
            ]
        if query and "container_memory_working_set_bytes" in query:
            return [
                {
                    "metric": {"namespace": "ns-1", "pod": "pod-A", "container": "web", "node": "node-1"},
                    "values": [[1672567500, str(120 * 1024**2)]],
                },
                {
                    "metric": {
                        "namespace": "ns-1",
                        "pod": "pod-A",
                        "container": "telemetry-sidecar",
                        "node": "node-1",
                    },
                    "values": [[1672567500, str(40 * 1024**2)]],
                },
            ]
        return []

    mock_prometheus_collector.collect_range = AsyncMock(side_effect=collect_range)
    data_processor.estimator.calculate_node_energy.return_value = [
        EnergyMetric(pod_name="pod-A", namespace="ns-1", joules=1_000_000, node="node-1")
    ]
    data_processor.estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 10, "maxWatts": 50}}
    mock_node_repository.get_latest_snapshots_before.return_value = [SAMPLE_NODE_INFO["node-1"]]
    mock_node_repository.get_snapshots.return_value = []

    metrics = await data_processor.run_range(start, end)
    containers = [metric for metric in metrics if metric.container_name]

    assert {metric.container_name for metric in containers} == {"web", "telemetry-sidecar"}
    assert {metric.container_name: metric.cpu_request for metric in containers} == {
        "web": 500,
        "telemetry-sidecar": 100,
    }
    assert {metric.container_name: metric.cpu_usage_millicores for metric in containers} == {
        "web": 20,
        "telemetry-sidecar": 10,
    }


@pytest.mark.asyncio
@patch("greenkube.core.node_zone_mapper.get_emaps_zone_from_cloud_zone")
async def test_run_range_uses_metrics_api_when_prometheus_drops_container_labels(
    mock_translator,
    data_processor,
    mock_prometheus_collector,
    mock_pod_collector,
    mock_node_repository,
):
    now = datetime.now(timezone.utc)
    aligned_end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, tz=timezone.utc)
    start = aligned_end - timedelta(minutes=5)
    end = aligned_end
    mock_translator.return_value = "FR"
    data_processor._range_processor._config = Config(KUBERNETES_METRICS_API_ENABLED=True)
    data_processor._range_processor.combined_metrics_repository.read_combined_metrics = AsyncMock(return_value=[])
    data_processor._range_processor.opencost_collector.collect_range = AsyncMock(return_value=[])
    mock_prometheus_collector.collect_pod_storage = AsyncMock(return_value={})
    mock_node_repository.get_latest_snapshots_before.return_value = [SAMPLE_NODE_INFO["node-1"]]
    mock_node_repository.get_snapshots.return_value = []
    mock_pod_collector.collect = AsyncMock(
        return_value=[
            PodMetric(
                pod_name="pod-A",
                namespace="ns-1",
                container_name="web",
                cpu_request=500,
                memory_request=256 * 1024**2,
                owner_kind="Deployment",
                owner_name="api",
            ),
            PodMetric(
                pod_name="pod-A",
                namespace="ns-1",
                container_name="telemetry-sidecar",
                cpu_request=100,
                memory_request=64 * 1024**2,
                owner_kind="Deployment",
                owner_name="api",
            ),
        ]
    )

    async def collect_range(*, start, end, step=None, query=None, namespace=None):
        if query and "container_cpu_usage_seconds_total" in query:
            return [
                {
                    "metric": {"namespace": "ns-1", "pod": "pod-A", "node": "node-1"},
                    "values": [[end.timestamp(), "0.03"]],
                }
            ]
        return []

    mock_prometheus_collector.collect_range = AsyncMock(side_effect=collect_range)
    data_processor.estimator.calculate_node_energy.return_value = [
        EnergyMetric(pod_name="pod-A", namespace="ns-1", joules=1_000_000, node="node-1")
    ]
    data_processor.estimator.instance_profiles = {"m5.large": {"vcores": 2, "minWatts": 10, "maxWatts": 50}}

    class FakeMetricsServerCollector(MetricsServerCollector):
        async def collect(self):
            return [
                ContainerResourceUsage(
                    namespace="ns-1",
                    pod_name="pod-A",
                    container_name="web",
                    cpu_usage_millicores=20,
                    memory_usage_bytes=120 * 1024**2,
                    timestamp=now,
                    duration_seconds=15,
                ),
                ContainerResourceUsage(
                    namespace="ns-1",
                    pod_name="pod-A",
                    container_name="telemetry-sidecar",
                    cpu_usage_millicores=10,
                    memory_usage_bytes=40 * 1024**2,
                    timestamp=now,
                    duration_seconds=15,
                ),
            ]

    data_processor._range_processor._metrics_server_collector = FakeMetricsServerCollector()

    metrics = await data_processor.run_range(start, end)
    containers = {metric.container_name: metric for metric in metrics if metric.container_name}

    assert set(containers) == {"web", "telemetry-sidecar"}
    assert containers["web"].cpu_usage_millicores == 20
    assert containers["web"].memory_usage_bytes == 120 * 1024**2
    assert containers["telemetry-sidecar"].cpu_usage_millicores == 10
    assert containers["telemetry-sidecar"].memory_usage_bytes == 40 * 1024**2
