# tests/core/test_processor.py

import pytest
from unittest.mock import MagicMock, patch
from src.greenkube.core.processor import DataProcessor
from src.greenkube.models.metrics import EnergyMetric, CostMetric, CombinedMetric
from src.greenkube.core.calculator import CarbonCalculationResult
# --- Import the config object for default values ---
from src.greenkube.core.config import config
# -------------------------------------------------

# Sample data for mocking collectors
SAMPLE_ENERGY_METRICS = [
    EnergyMetric(pod_name='pod-A', namespace='ns-1', joules=1000000, timestamp='2023-10-27T10:00:00Z', node='node-1', region='us-east-1'),
    EnergyMetric(pod_name='pod-B', namespace='ns-2', joules=2000000, timestamp='2023-10-27T10:05:00Z', node='node-2', region='eu-west-1'),
    EnergyMetric(pod_name='pod-C', namespace='ns-1', joules=500000,  timestamp='2023-10-27T10:02:00Z', node='node-1', region='us-east-1'), # Pod for missing cost
    EnergyMetric(pod_name='pod-D', namespace='ns-3', joules=300000,  timestamp='2023-10-27T10:03:00Z', node='node-unknown', region='unknown'), # Pod for missing node zone
]

SAMPLE_COST_METRICS = [
    CostMetric(pod_name='pod-A', namespace='ns-1', cpu_cost=0.1, ram_cost=0.2, total_cost=0.3, timestamp='2023-10-27T10:00:00Z'),
    CostMetric(pod_name='pod-B', namespace='ns-2', cpu_cost=0.4, ram_cost=0.5, total_cost=0.9, timestamp='2023-10-27T10:05:00Z'),
    # No cost metric for pod-C intentionally
]

SAMPLE_NODE_ZONES = {
    'node-1': 'gcp-us-east1-a', # Maps to US-CISO-NE
    'node-2': 'aws-eu-west-1b', # Maps to IE
    # No mapping for node-unknown intentionally
}

SAMPLE_CALCULATION_RESULT_A = CarbonCalculationResult(co2e_grams=50.0, grid_intensity=100.0)
SAMPLE_CALCULATION_RESULT_B = CarbonCalculationResult(co2e_grams=150.0, grid_intensity=120.0)
SAMPLE_CALCULATION_RESULT_C = CarbonCalculationResult(co2e_grams=25.0, grid_intensity=100.0) # Using same intensity as A for simplicity
SAMPLE_CALCULATION_RESULT_D = CarbonCalculationResult(co2e_grams=10.0, grid_intensity=config.DEFAULT_INTENSITY) # Uses default zone -> default intensity


# --- Fixtures for Mocks ---
@pytest.fixture
def mock_kepler_collector():
    """ Provides a mock KeplerCollector. """
    mock = MagicMock()
    mock.collect.return_value = SAMPLE_ENERGY_METRICS
    return mock

@pytest.fixture
def mock_opencost_collector():
    """ Provides a mock OpenCostCollector. """
    mock = MagicMock()
    mock.collect.return_value = SAMPLE_COST_METRICS
    return mock

@pytest.fixture
def mock_node_collector():
    """ Provides a mock NodeCollector. """
    mock = MagicMock()
    mock.collect.return_value = SAMPLE_NODE_ZONES
    return mock

# --- ADDED: Fixtures for repository and calculator ---
@pytest.fixture
def mock_repository():
    """ Provides a mock CarbonIntensityRepository. """
    mock = MagicMock()
    # Configure mock behavior if needed for specific tests
    return mock

@pytest.fixture
def mock_calculator():
    """ Provides a mock CarbonCalculator. """
    mock = MagicMock()
    # Configure a side effect to return different results based on input
    def calculate_side_effect(joules, zone, timestamp):
        # Determine pod name based on joules for simplified mapping in tests
        pod_name = None
        if joules == 1000000: pod_name = 'pod-A'
        elif joules == 2000000: pod_name = 'pod-B'
        elif joules == 500000: pod_name = 'pod-C'
        elif joules == 300000: pod_name = 'pod-D'

        if pod_name == 'pod-A': return SAMPLE_CALCULATION_RESULT_A
        if pod_name == 'pod-B': return SAMPLE_CALCULATION_RESULT_B
        if pod_name == 'pod-C': return SAMPLE_CALCULATION_RESULT_C
        if pod_name == 'pod-D': return SAMPLE_CALCULATION_RESULT_D
        print(f"WARN: Mock calculator received unexpected joules: {joules}") # Added warning
        return CarbonCalculationResult(0.0, 0.0) # Default fallback

    mock.calculate_emissions.side_effect = calculate_side_effect
    mock.pue = config.DEFAULT_PUE # Set the pue attribute as the processor reads it
    return mock
# ------------------------------------------------------

@pytest.fixture
def data_processor(mock_kepler_collector, mock_opencost_collector, mock_node_collector, mock_repository, mock_calculator):
    """
    Provides an instance of DataProcessor with mocked dependencies.
    Injects the new mock_repository and mock_calculator.
    """
    return DataProcessor(
        kepler_collector=mock_kepler_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector,
        repository=mock_repository,       # Pass the mock repository
        calculator=mock_calculator        # Pass the mock calculator
    )

# --- Test Cases ---

# --- Patch the CORRECT translator function ---
@patch('src.greenkube.core.processor.get_emaps_zone_from_cloud_zone')
# -------------------------------------------
def test_processor_combines_data_correctly(mock_translator, data_processor, mock_calculator):
    """
    Tests the main success path: combines data from all sources correctly.
    """
    # Arrange
    # Configure the translator mock
    def translator_side_effect(region):
        if region == 'gcp-us-east1-a': return 'US-CISO-NE'
        if region == 'aws-eu-west-1b': return 'IE'
        return None # Default for unknown regions like 'unknown'
    mock_translator.side_effect = translator_side_effect

    # Configure calculator mock results (side effect in fixture handles this now)


    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4 # Expect all 4 metrics to be processed

    # Detailed checks for pod-A (has all data)
    metric_a = next(m for m in combined_results if m.pod_name == 'pod-A')
    assert metric_a.namespace == 'ns-1'
    assert metric_a.total_cost == SAMPLE_COST_METRICS[0].total_cost
    assert metric_a.co2e_grams == SAMPLE_CALCULATION_RESULT_A.co2e_grams
    assert metric_a.grid_intensity == SAMPLE_CALCULATION_RESULT_A.grid_intensity
    assert metric_a.pue == config.DEFAULT_PUE

    # Detailed checks for pod-B (has all data)
    metric_b = next(m for m in combined_results if m.pod_name == 'pod-B')
    assert metric_b.namespace == 'ns-2'
    assert metric_b.total_cost == SAMPLE_COST_METRICS[1].total_cost
    assert metric_b.co2e_grams == SAMPLE_CALCULATION_RESULT_B.co2e_grams
    assert metric_b.grid_intensity == SAMPLE_CALCULATION_RESULT_B.grid_intensity
    assert metric_b.pue == config.DEFAULT_PUE

    # Check calculator calls precisely
    assert mock_calculator.calculate_emissions.call_count == 4
    # Call 1 (pod-A)
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[0].joules,
        zone='US-CISO-NE', # Result of translation for node-1's zone
        timestamp=SAMPLE_ENERGY_METRICS[0].timestamp
    )
    # Call 2 (pod-B)
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[1].joules,
        zone='IE', # Result of translation for node-2's zone
        timestamp=SAMPLE_ENERGY_METRICS[1].timestamp
    )
     # Call 3 (pod-C) - uses pod-A's zone as it's on the same node
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[2].joules,
        zone='US-CISO-NE',
        timestamp=SAMPLE_ENERGY_METRICS[2].timestamp
    )
    # Call 4 (pod-D) - uses default zone
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[3].joules,
        zone=config.DEFAULT_ZONE, # Used because node/zone mapping failed
        timestamp=SAMPLE_ENERGY_METRICS[3].timestamp
    )


# --- Patch the CORRECT translator function ---
@patch('src.greenkube.core.processor.get_emaps_zone_from_cloud_zone')
# -------------------------------------------
def test_processor_estimates_missing_cost_data(mock_translator, data_processor, mock_calculator):
    """
    Tests that the processor uses the default cost when OpenCost data is missing for a pod,
    but still calculates emissions.
    """
    # Arrange
    def translator_side_effect(region):
        if region == 'gcp-us-east1-a': return 'US-CISO-NE'
        if region == 'aws-eu-west-1b': return 'IE'
        return None
    mock_translator.side_effect = translator_side_effect

    # Define return values for the calculator mock for this specific test
    # (side effect in fixture handles this now)


    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4 # Still expect 4 results

    # Find the metric for pod-C (which had missing cost data)
    metric_c = next((m for m in combined_results if m.pod_name == 'pod-C'), None)
    assert metric_c is not None, "Metric for pod-C should exist"

    # Verify that default cost was used, but calculation happened
    assert metric_c.total_cost == config.DEFAULT_COST
    assert metric_c.namespace == 'ns-1' # Check other fields are populated
    assert metric_c.co2e_grams == SAMPLE_CALCULATION_RESULT_C.co2e_grams # Ensure calculation was done
    assert metric_c.grid_intensity == SAMPLE_CALCULATION_RESULT_C.grid_intensity
    assert metric_c.pue == config.DEFAULT_PUE

    # Verify calculator was called for pod-C
    mock_calculator.calculate_emissions.assert_any_call(
        joules=SAMPLE_ENERGY_METRICS[2].joules,
        zone='US-CISO-NE', # On node-1 -> gcp-us-east1-a -> US-CISO-NE
        timestamp=SAMPLE_ENERGY_METRICS[2].timestamp
    )


@patch('src.greenkube.core.processor.NodeCollector') # Patch NodeCollector instantiation
# --- Patch the CORRECT translator function ---
@patch('src.greenkube.core.processor.get_emaps_zone_from_cloud_zone')
# -------------------------------------------
def test_processor_uses_default_zone_when_node_zone_missing(mock_translator, mock_node_collector_class, mock_kepler_collector, mock_opencost_collector, mock_repository, mock_calculator):
    """
    Tests that the processor uses the default zone for calculations
    when the NodeCollector fails or doesn't provide a zone for a node.
    """
    # Arrange
    # Configure the NodeCollector mock *instance* to return an empty dict
    mock_node_collector_instance = mock_node_collector_class.return_value
    mock_node_collector_instance.collect.return_value = {} # Simulate no zones found

    # Need to instantiate processor manually here because the fixture uses the class mock incorrectly
    data_processor = DataProcessor(
        kepler_collector=mock_kepler_collector,
        opencost_collector=mock_opencost_collector,
        node_collector=mock_node_collector_instance, # Use the configured instance
        repository=mock_repository,
        calculator=mock_calculator
    )


    # Translator won't be called effectively if default zone is used, but mock it anyway
    mock_translator.return_value = None

    # Configure calculator mock results (side effect in fixture handles this now)


    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 4 # Should still process all metrics

    # Verify that the calculator was called with the DEFAULT_ZONE for all pods
    assert mock_calculator.calculate_emissions.call_count == 4
    for energy_metric in SAMPLE_ENERGY_METRICS:
        mock_calculator.calculate_emissions.assert_any_call(
            joules=energy_metric.joules,
            zone=config.DEFAULT_ZONE, # <<< ASSERTION: Default zone used
            timestamp=energy_metric.timestamp
        )

    # Verify NodeCollector was called
    mock_node_collector_instance.collect.assert_called_once()
    # Translator should not have been called since node zones were missing
    mock_translator.assert_not_called()


def test_processor_handles_kepler_failure(data_processor, mock_kepler_collector, mock_opencost_collector, mock_node_collector, mock_calculator):
    """
    Tests that the processor continues (returning empty results) if Kepler fails.
    """
    # Arrange
    mock_kepler_collector.collect.side_effect = Exception("Kepler API down")

    # Act
    combined_results = data_processor.run()

    # Assert
    assert combined_results == [] # No energy data, so no combined metrics
    # Check that other collectors were still called (or decide if they shouldn't be)
    mock_node_collector.collect.assert_called_once()
    mock_opencost_collector.collect.assert_called_once()
    mock_calculator.calculate_emissions.assert_not_called() # No energy data to calculate


def test_processor_handles_opencost_failure(data_processor, mock_opencost_collector, mock_node_collector, mock_calculator):
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
    assert len(combined_results) == 4 # Should still process all energy metrics

    # Check that default cost was used for all
    for metric in combined_results:
        assert metric.total_cost == config.DEFAULT_COST

    # Verify calculator was still called 4 times
    assert mock_calculator.calculate_emissions.call_count == 4
    mock_node_collector.collect.assert_called_once()


# --- Patch the CORRECT translator function ---
@patch('src.greenkube.core.processor.get_emaps_zone_from_cloud_zone')
# -------------------------------------------
def test_processor_handles_calculator_failure(mock_translator, data_processor, mock_calculator):
    """
    Tests that the processor skips a metric if the calculator fails for it,
    but continues processing others.
    """
    # Arrange
    # Configure the translator mock
    def translator_side_effect(region):
        if region == 'gcp-us-east1-a': return 'US-CISO-NE'
        if region == 'aws-eu-west-1b': return 'IE'
        return None
    mock_translator.side_effect = translator_side_effect

    # Make calculator fail only for the second call (pod-B)
    mock_calculator.calculate_emissions.side_effect = [
        SAMPLE_CALCULATION_RESULT_A,
        Exception("Calculation failed!"), # Failure for pod-B
        SAMPLE_CALCULATION_RESULT_C,
        SAMPLE_CALCULATION_RESULT_D
    ]

    # Act
    combined_results = data_processor.run()

    # Assert
    assert len(combined_results) == 3 # Only 3 metrics should be successfully combined
    assert 'pod-B' not in [m.pod_name for m in combined_results] # pod-B should be skipped
    assert 'pod-A' in [m.pod_name for m in combined_results]
    assert 'pod-C' in [m.pod_name for m in combined_results]
    assert 'pod-D' in [m.pod_name for m in combined_results]

    # Verify calculator was called 4 times (even though one failed)
    assert mock_calculator.calculate_emissions.call_count == 4

