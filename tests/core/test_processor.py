# tests/core/test_processor.py

from unittest.mock import MagicMock, patch

import pytest

from greenkube.core.calculator import CarbonCalculationResult
from greenkube.core.config import config
from greenkube.core.processor import DataProcessor
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.metrics import (
    CostMetric,
    EnergyMetric,
    PodMetric,
)
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import PrometheusMetric

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
    mock.collect.return_value = SAMPLE_ENERGY_METRICS
    return mock


@pytest.fixture
def mock_prometheus_collector():
    """Provides a mock PrometheusCollector that returns a PrometheusMetric placeholder."""
    mock = MagicMock()
    mock.collect.return_value = MagicMock(spec=PrometheusMetric)
    return mock


@pytest.fixture
def mock_opencost_collector():
    """Provides a mock OpenCostCollector."""
    mock = MagicMock()
    mock.collect.return_value = SAMPLE_COST_METRICS
    return mock


@pytest.fixture
def mock_node_collector():
    """Provides a mock NodeCollector that returns NodeInfo objects."""
    mock = MagicMock()
    mock.collect.return_value = SAMPLE_NODE_INFO
    mock.collect_instance_types.return_value = {"node-1": "m5.large", "node-2": "m5.large"}
    return mock


@pytest.fixture
def mock_pod_collector():
    """Provides a mock PodCollector."""
    mock = MagicMock()
    mock.collect.return_value = []
    return mock


@pytest.fixture
def mock_repository():
    """Provides a mock CarbonIntensityRepository."""
    mock = MagicMock()
    # Configure mock behavior if needed for specific tests
    return mock


@pytest.fixture
def mock_calculator():
    """Provides a mock CarbonCalculator."""
    mock = MagicMock()

    # Configure a side effect to return different results based on input
    def calculate_side_effect(joules, zone, timestamp):
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
    return mock


@pytest.fixture
def mock_electricity_maps_collector():
    """Provides a mock ElectricityMapsCollector."""
    mock = MagicMock()
    return mock


@pytest.fixture
def mock_node_repository():
    """Provides a mock NodeRepository."""
    mock = MagicMock()
    mock.get_latest_snapshots_before.return_value = []
    mock.get_snapshots.return_value = []
    return mock


@pytest.fixture
def data_processor(
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_pod_collector,
    mock_electricity_maps_collector,
    mock_repository,
    mock_node_repository,
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

    return DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_maps_collector=mock_electricity_maps_collector,
        repository=mock_repository,  # Pass the mock repository
        node_repository=mock_node_repository,
        calculator=mock_calculator,  # Pass the mock calculator
        estimator=estimator_mock,
    )


# --- Test Cases ---


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_combines_data_correctly(mock_translator, data_processor, mock_calculator):
    """
    Tests the main success path: combines data from all sources correctly.
    """

    # Arrange
    # Configure the translator mock
    def translator_side_effect(region, provider=None):
        if region == "gcp-us-east1-a":
            return "US-CISO-NE"
        if region == "aws-eu-west-1b":
            return "IE"
        return None  # Default for unknown regions like 'unknown'

    mock_translator.side_effect = translator_side_effect

    # Configure calculator mock results (side effect in fixture handles this now)

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4  # Expect all 4 metrics to be processed

    # Detailed checks for pod-A (has all data)
    metric_a = next(m for m in combined_results if m.pod_name == "pod-A")
    assert metric_a.namespace == "ns-1"
    assert metric_a.total_cost == SAMPLE_COST_METRICS[0].total_cost
    assert metric_a.co2e_grams == SAMPLE_CALCULATION_RESULT_A.co2e_grams
    assert metric_a.grid_intensity == SAMPLE_CALCULATION_RESULT_A.grid_intensity
    assert metric_a.grid_intensity == SAMPLE_CALCULATION_RESULT_A.grid_intensity
    assert metric_a.pue == 1.09  # GCP PUE

    # Detailed checks for pod-B (has all data)
    metric_b = next(m for m in combined_results if m.pod_name == "pod-B")
    assert metric_b.namespace == "ns-2"
    assert metric_b.total_cost == SAMPLE_COST_METRICS[1].total_cost
    assert metric_b.co2e_grams == SAMPLE_CALCULATION_RESULT_B.co2e_grams
    assert metric_b.grid_intensity == SAMPLE_CALCULATION_RESULT_B.grid_intensity
    assert metric_b.pue == 1.15  # AWS PUE

    # Check calculator calls precisely
    assert mock_calculator.calculate_emissions.call_count == 4
    # Call 1 (pod-A)
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[0].joules,
        zone="US-CISO-NE",  # Result of translation for node-1's zone
        timestamp=SAMPLE_ENERGY_METRICS[0].timestamp,
    )
    # Call 2 (pod-B)
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[1].joules,
        zone="IE",  # Result of translation for node-2's zone
        timestamp=SAMPLE_ENERGY_METRICS[1].timestamp,
    )
    # Call 3 (pod-C) - uses pod-A's zone as it's on the same node
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[2].joules,
        zone="US-CISO-NE",
        timestamp=SAMPLE_ENERGY_METRICS[2].timestamp,
    )
    # Call 4 (pod-D) - uses default zone
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[3].joules,
        zone=config.DEFAULT_ZONE,  # Used because node/zone mapping failed
        timestamp=SAMPLE_ENERGY_METRICS[3].timestamp,
    )


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_estimates_missing_cost_data(mock_translator, data_processor, mock_calculator):
    """
    Tests that the processor uses the default cost when OpenCost data is missing for a pod,
    but still calculates emissions.
    """

    # Arrange
    def translator_side_effect(region, provider=None):
        if region == "gcp-us-east1-a":
            return "US-CISO-NE"
        if region == "aws-eu-west-1b":
            return "IE"
        return None

    mock_translator.side_effect = translator_side_effect

    # Define return values for the calculator mock for this specific test
    # (side effect in fixture handles this now)

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4  # Still expect 4 results

    # Find the metric for pod-C (which had missing cost data)
    metric_c = next((m for m in combined_results if m.pod_name == "pod-C"), None)
    assert metric_c is not None, "Metric for pod-C should exist"

    # Verify that default cost was used, but calculation happened
    assert metric_c.total_cost == config.DEFAULT_COST
    assert metric_c.namespace == "ns-1"  # Check other fields are populated
    assert metric_c.co2e_grams == SAMPLE_CALCULATION_RESULT_C.co2e_grams  # Ensure calculation was done
    assert metric_c.grid_intensity == SAMPLE_CALCULATION_RESULT_C.grid_intensity
    assert metric_c.pue == 1.09  # GCP PUE

    # Verify calculator was called for pod-C
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[2].joules,
        zone="US-CISO-NE",  # On node-1 -> gcp-us-east1-a -> US-CISO-NE
        timestamp=SAMPLE_ENERGY_METRICS[2].timestamp,
    )


@patch("greenkube.core.processor.NodeCollector")  # Patch NodeCollector instantiation
@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_uses_default_zone_when_node_zone_missing(
    mock_translator,
    mock_node_collector_class,
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_repository,
    mock_calculator,
):
    """
    Tests that the processor uses the default zone for calculations
    when the NodeCollector fails or doesn't provide a zone for a node.
    """
    # Arrange
    # Configure the NodeCollector mock *instance* to return an empty dict
    mock_node_collector_instance = mock_node_collector_class.return_value
    mock_node_collector_instance.collect.return_value = {}  # Simulate no zones found
    mock_node_collector_instance.collect_instance_types.return_value = {}  # Simulate no instance types

    # Need to instantiate processor manually here because the fixture uses the class mock incorrectly
    data_processor = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector_instance,  # Use the configured instance
        pod_collector=MagicMock(),
        electricity_maps_collector=MagicMock(),
        repository=mock_repository,
        node_repository=MagicMock(),
        calculator=mock_calculator,
        estimator=MagicMock(estimate=lambda *_: SAMPLE_ENERGY_METRICS),
    )

    # Translator won't be called effectively if default zone is used, but mock it anyway
    mock_translator.return_value = None

    # Configure calculator mock results (side effect in fixture handles this now)

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4  # Should still process all metrics

    # Verify that the calculator was called with the DEFAULT_ZONE for all pods
    assert mock_calculator.calculate_emissions.call_count == 4
    for energy_metric in SAMPLE_ENERGY_METRICS:
        mock_calculator.calculate_emissions.assert_any_call(
            joules=energy_metric.joules,
            zone=config.DEFAULT_ZONE,  # <<< ASSERTION: Default zone used
            timestamp=energy_metric.timestamp,
        )

    # Verify NodeCollector was called
    mock_node_collector_instance.collect.assert_called_once()
    # Translator should not have been called since node zones were missing
    mock_translator.assert_not_called()


def test_processor_handles_prometheus_failure(
    data_processor,
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_calculator,
):
    """
    Tests that the processor continues (returning empty results) if Prometheus collection/estimation fails.
    """
    # Arrange
    mock_prometheus_collector.collect.side_effect = Exception("Prometheus API down")

    # Act
    combined_results = data_processor.run()

    # Assert
    assert combined_results == []  # No energy data, so no combined metrics
    # Check that other collectors were still called (or decide if they shouldn't be)
    mock_node_collector.collect.assert_called_once()
    mock_opencost_collector.collect.assert_called_once()
    mock_calculator.calculate_emissions.assert_not_called()  # No energy data to calculate


def test_processor_handles_opencost_failure(
    data_processor, mock_opencost_collector, mock_node_collector, mock_calculator
):
    """
    Tests that the processor continues using default costs if OpenCost fails.
    """
    # Arrange
    mock_opencost_collector.collect.side_effect = Exception("OpenCost API down")

    # Reconfigure calculator mock side effect for this specific scenario
    # (side effect in fixture handles this now)

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4  # Should still process all energy metrics

    # Check that default cost was used for all
    for metric in combined_results:
        assert metric.total_cost == config.DEFAULT_COST

    # Verify calculator was still called 4 times
    assert mock_calculator.calculate_emissions.call_count == 4
    mock_node_collector.collect.assert_called_once()


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_handles_calculator_failure(mock_translator, data_processor, mock_calculator):
    """
    Tests that the processor skips a metric if the calculator fails for it,
    but continues processing others.
    """

    # Arrange
    # Configure the translator mock
    def translator_side_effect(region, provider=None):
        if region == "gcp-us-east1-a":
            return "US-CISO-NE"
        if region == "aws-eu-west-1b":
            return "IE"
        return None

    mock_translator.side_effect = translator_side_effect

    # Make calculator fail only for the second call (pod-B)
    mock_calculator.calculate_emissions.side_effect = [
        SAMPLE_CALCULATION_RESULT_A,
        Exception("Calculation failed!"),  # Failure for pod-B
        SAMPLE_CALCULATION_RESULT_C,
        SAMPLE_CALCULATION_RESULT_D,
    ]

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 3  # Only 3 metrics should be successfully combined
    assert "pod-B" not in [m.pod_name for m in combined_results]  # pod-B should be skipped
    assert "pod-A" in [m.pod_name for m in combined_results]
    assert "pod-C" in [m.pod_name for m in combined_results]
    assert "pod-D" in [m.pod_name for m in combined_results]

    # Verify calculator was called 4 times (even though one failed)
    assert mock_calculator.calculate_emissions.call_count == 4


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_aggregates_pod_requests(
    mock_translator,
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_repository,
    mock_calculator,
):
    """Ensure PodCollector request aggregation is applied to combined metrics."""
    # Arrange: create a pod_collector mock that returns per-container PodMetric entries
    pod_metrics = [
        PodMetric(
            pod_name="pod-A",
            namespace="ns-1",
            container_name="c1",
            cpu_request=100,
            memory_request=128 * 1024 * 1024,
        ),
        PodMetric(
            pod_name="pod-A",
            namespace="ns-1",
            container_name="c2",
            cpu_request=200,
            memory_request=64 * 1024 * 1024,
        ),
    ]

    from unittest.mock import MagicMock

    mock_pod_collector = MagicMock()
    mock_pod_collector.collect.return_value = pod_metrics

    dp = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_maps_collector=MagicMock(),
        repository=mock_repository,
        node_repository=MagicMock(),
        calculator=mock_calculator,
        estimator=MagicMock(estimate=lambda *_: SAMPLE_ENERGY_METRICS),
    )

    # Translator stub
    mock_translator.return_value = "US-CISO-NE"

    # Act
    combined = dp.run()

    # Assert: find pod-A and verify cpu_request and memory_request are summed
    metric_a = next((m for m in combined if m.pod_name == "pod-A"), None)
    assert metric_a is not None
    assert metric_a.cpu_request == 300
    assert metric_a.memory_request == (128 + 64) * 1024 * 1024


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_handles_missing_pod_requests(
    mock_translator,
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_repository,
    mock_calculator,
):
    """If PodCollector returns empty or fails, cpu_request and memory_request should be zero for combined metrics."""
    from unittest.mock import MagicMock

    mock_pod_collector = MagicMock()
    mock_pod_collector.collect.return_value = []

    dp = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=mock_pod_collector,
        electricity_maps_collector=MagicMock(),
        repository=mock_repository,
        node_repository=MagicMock(),
        calculator=mock_calculator,
        estimator=MagicMock(estimate=lambda *_: SAMPLE_ENERGY_METRICS),
    )

    mock_translator.return_value = "US-CISO-NE"

    combined = dp.run()
    for m in combined:
        assert m.cpu_request == 0 or isinstance(m.cpu_request, int)
        assert m.memory_request == 0 or isinstance(m.memory_request, int)


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_processor_nodecollector_instance_type_fallback(
    mock_translator,
    mock_prometheus_collector,
    mock_opencost_collector,
    mock_node_collector,
    mock_repository,
    mock_calculator,
):
    """
    If Prometheus returns no node instance types, DataProcessor should call
    NodeCollector.collect_instance_types() and supply those to the estimator.
    """
    from unittest.mock import MagicMock

    from greenkube.models.prometheus_metrics import (
        NodeInstanceType,
        PrometheusMetric,
    )

    # Build a PrometheusMetric with pod_cpu_usage but empty node_instance_types
    prom_metric = PrometheusMetric()
    prom_metric.pod_cpu_usage = []
    prom_metric.node_instance_types = []

    # Configure the mocked PrometheusCollector to return this metric
    mock_prometheus_collector.collect.return_value = prom_metric

    # Configure the NodeCollector instance to return instance types via collect_instance_types
    node_instance_map = {"node-1": "m5.large"}
    mock_node_instance_collector = mock_node_collector
    mock_node_instance_collector.collect_instance_types.return_value = node_instance_map

    # Spy on the estimator to see the PrometheusMetric it receives
    estimator_spy = MagicMock()
    estimator_spy.estimate.return_value = []

    # Ensure translator returns a string to satisfy Pydantic model
    mock_translator.return_value = "US-TEST"

    dp = DataProcessor(
        prometheus_collector=mock_prometheus_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        pod_collector=MagicMock(),
        electricity_maps_collector=MagicMock(),
        repository=mock_repository,
        node_repository=MagicMock(),
        calculator=mock_calculator,
        estimator=estimator_spy,
    )

    # Run
    dp.run()

    # Assert NodeCollector.collect_instance_types was called
    mock_node_collector.collect_instance_types.assert_called_once()

    # Assert estimator was called with a PrometheusMetric that now has node_instance_types populated
    called_metric = estimator_spy.estimate.call_args[0][0]
    assert isinstance(called_metric, PrometheusMetric)
    assert any(isinstance(nt, NodeInstanceType) for nt in called_metric.node_instance_types)
    assert called_metric.node_instance_types[0].node == "node-1"
    assert called_metric.node_instance_types[0].instance_type == "m5.large"


@patch("greenkube.core.processor.get_emaps_zone_from_cloud_zone")
def test_run_range_uses_historical_node_data(
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
            "values": [[1672567200, "1.0"]],  # 10:00:00
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
        {"pod_name": "pod-A", "namespace": "ns-1", "joules": 100, "node": "node-1"}
    ]

    # We also need to mock instance_profiles in estimator if we want profile lookup to work,
    # OR we can rely on the fact that processor calls profile_for_node which uses estimator.instance_profiles
    data_processor.estimator.instance_profiles = {
        "t3.medium": {"vcores": 2, "minWatts": 10, "maxWatts": 20},
        "t3.large": {"vcores": 4, "minWatts": 20, "maxWatts": 40},
    }

    # Act
    # Act
    data_processor.run_range(start, end)

    # Assert
    # Verify that get_latest_snapshots_before and get_snapshots were called
    mock_node_repository.get_latest_snapshots_before.assert_called_once()
    mock_node_repository.get_snapshots.assert_called_once()

    # Verify that calculate_node_energy was called.
    # We can check the 'node_profile' argument passed to it.
    # Since we have one data point at 10:00, it should use the initial snapshot (t3.medium)

    calls = data_processor.estimator.calculate_node_energy.call_args_list
    assert len(calls) > 0

    # Check the profile used in the call corresponding to the timestamp
    # The timestamp in collect_range is 1672567200 (10:00:00)
    # So it should use t3.medium profile
    args, kwargs = calls[0]
    assert kwargs["node_name"] == "node-1"
    assert kwargs["node_profile"]["vcores"] == 2
