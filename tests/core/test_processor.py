# tests/core/test_processor.py
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Import necessary classes (adjust paths if needed)
from src.greenkube.models.metrics import EnergyMetric, CostMetric, CombinedMetric
from src.greenkube.core.processor import DataProcessor
from src.greenkube.core.calculator import CarbonCalculationResult
# --- Import the config object ---
from src.greenkube.core.config import config
# --------------------------------

# Create test data
TIMESTAMP_STR = "2023-10-27T10:00:00Z"
TIMESTAMP_DT = datetime.fromisoformat(TIMESTAMP_STR.replace("Z", "+00:00"))

FAKE_ENERGY_DATA = [
    EnergyMetric(pod_name="pod-A", namespace="ns-1", joules=1000, timestamp=TIMESTAMP_DT, node="node-1", region="europe-west9-a"),
    EnergyMetric(pod_name="pod-B", namespace="ns-2", joules=2000, timestamp=TIMESTAMP_DT, node="node-1", region="europe-west9-a"),
    EnergyMetric(pod_name="pod-C", namespace="ns-1", joules=500, timestamp=TIMESTAMP_DT, node="node-1", region="europe-west9-a"), # Corresponding cost missing
]

FAKE_COST_DATA = [
    CostMetric(pod_name="pod-A", namespace="ns-1", cpu_cost=0.1, ram_cost=0.2, total_cost=0.3, timestamp=TIMESTAMP_DT),
    CostMetric(pod_name="pod-B", namespace="ns-2", cpu_cost=0.4, ram_cost=0.5, total_cost=0.9, timestamp=TIMESTAMP_DT),
    # No cost for pod-C
]

# Expected results from the calculator (assuming 150 gCO2e/kWh intensity and PUE 1.5)
FAKE_CALC_RESULT_A = CarbonCalculationResult(co2e_grams=0.0625, grid_intensity=150.0)
FAKE_CALC_RESULT_B = CarbonCalculationResult(co2e_grams=0.125, grid_intensity=150.0)
FAKE_CALC_RESULT_C = CarbonCalculationResult(co2e_grams=0.03125, grid_intensity=150.0)


@pytest.fixture
def mock_dependencies():
    """ Creates mocks for DataProcessor dependencies. """
    mock_kepler = MagicMock()
    mock_opencost = MagicMock()
    mock_calculator = MagicMock()
    return mock_kepler, mock_opencost, mock_calculator

# Patch the NodeCollector where it's instantiated within the DataProcessor
@patch('src.greenkube.core.processor.NodeCollector')
def test_processor_combines_data_correctly(MockNodeCollector, mock_dependencies):
    """
    Verifies that the processor correctly combines energy, cost,
    and carbon data when all data points are available.
    """
    mock_kepler, mock_opencost, mock_calculator = mock_dependencies
    mock_node_collector_instance = MockNodeCollector.return_value

    mock_node_collector_instance.collect.return_value = ["europe-west9-a"]
    mock_kepler.collect.return_value = FAKE_ENERGY_DATA
    mock_opencost.collect.return_value = FAKE_COST_DATA
    mock_calculator.calculate_emissions.side_effect = [
        FAKE_CALC_RESULT_A, FAKE_CALC_RESULT_B, FAKE_CALC_RESULT_C
    ]
    mock_calculator.pue = 1.5 # Assuming PUE comes from calculator for now

    processor = DataProcessor(mock_kepler, mock_opencost, mock_calculator)
    combined_results = processor.run()

    mock_node_collector_instance.collect.assert_called_once()
    mock_kepler.collect.assert_called_once()
    mock_opencost.collect.assert_called_once()
    assert mock_calculator.calculate_emissions.call_count == 3
    # Assuming 'europe-west9-a' maps to 'FR' internally by the processor
    mock_calculator.calculate_emissions.assert_any_call(joules=1000, zone='FR', timestamp=TIMESTAMP_STR)
    mock_calculator.calculate_emissions.assert_any_call(joules=2000, zone='FR', timestamp=TIMESTAMP_STR)
    mock_calculator.calculate_emissions.assert_any_call(joules=500, zone='FR', timestamp=TIMESTAMP_STR)

    assert len(combined_results) == 3

    result_a = next(r for r in combined_results if r.pod_name == "pod-A")
    assert result_a.namespace == "ns-1"
    assert result_a.total_cost == 0.3
    assert result_a.co2e_grams == FAKE_CALC_RESULT_A.co2e_grams
    assert result_a.grid_intensity == FAKE_CALC_RESULT_A.grid_intensity
    assert result_a.pue == 1.5

    result_b = next(r for r in combined_results if r.pod_name == "pod-B")
    assert result_b.namespace == "ns-2"
    assert result_b.total_cost == 0.9
    assert result_b.co2e_grams == FAKE_CALC_RESULT_B.co2e_grams
    assert result_b.grid_intensity == FAKE_CALC_RESULT_B.grid_intensity
    assert result_b.pue == 1.5

    result_c = next(r for r in combined_results if r.pod_name == "pod-C")
    assert result_c.namespace == "ns-1"
    # --- Use config value for default cost ---
    assert result_c.total_cost == config.DEFAULT_COST
    # -----------------------------------------
    assert result_c.co2e_grams == FAKE_CALC_RESULT_C.co2e_grams
    assert result_c.grid_intensity == FAKE_CALC_RESULT_C.grid_intensity
    assert result_c.pue == 1.5

# Renamed test and updated logic
@patch('src.greenkube.core.processor.NodeCollector')
def test_processor_uses_default_zone_when_node_zone_missing(MockNodeCollector, mock_dependencies):
    """
    Verifies that the processor uses the config.DEFAULT_ZONE and continues processing
    when the NodeCollector fails to return a zone.
    """
    mock_kepler, mock_opencost, mock_calculator = mock_dependencies
    mock_node_collector_instance = MockNodeCollector.return_value

    mock_node_collector_instance.collect.return_value = [] # Simulate no zone found
    mock_kepler.collect.return_value = FAKE_ENERGY_DATA
    mock_opencost.collect.return_value = FAKE_COST_DATA
    mock_calculator.calculate_emissions.side_effect = [
        FAKE_CALC_RESULT_A, FAKE_CALC_RESULT_B, FAKE_CALC_RESULT_C
    ]
    mock_calculator.pue = 1.5

    processor = DataProcessor(mock_kepler, mock_opencost, mock_calculator)
    combined_results = processor.run()

    mock_node_collector_instance.collect.assert_called_once()
    mock_kepler.collect.assert_called_once()
    mock_opencost.collect.assert_called_once()
    assert mock_calculator.calculate_emissions.call_count == 3
    # --- Assert calculator is called using config.DEFAULT_ZONE ---
    mock_calculator.calculate_emissions.assert_any_call(joules=1000, zone=config.DEFAULT_ZONE, timestamp=TIMESTAMP_STR)
    mock_calculator.calculate_emissions.assert_any_call(joules=2000, zone=config.DEFAULT_ZONE, timestamp=TIMESTAMP_STR)
    mock_calculator.calculate_emissions.assert_any_call(joules=500, zone=config.DEFAULT_ZONE, timestamp=TIMESTAMP_STR)
    # ------------------------------------------------------------
    assert len(combined_results) == 3


@patch('src.greenkube.core.processor.NodeCollector')
def test_processor_estimates_missing_cost_data(MockNodeCollector, mock_dependencies):
    """
    Verifies that the processor uses config.DEFAULT_COST for energy metrics
    that lack corresponding cost metrics.
    """
    mock_kepler, mock_opencost, mock_calculator = mock_dependencies
    mock_node_collector_instance = MockNodeCollector.return_value

    mock_node_collector_instance.collect.return_value = ["europe-west9-a"]
    mock_kepler.collect.return_value = FAKE_ENERGY_DATA
    mock_opencost.collect.return_value = [ # Provide costs only for A and B
        CostMetric(pod_name="pod-A", namespace="ns-1", total_cost=0.3, timestamp=TIMESTAMP_DT),
        CostMetric(pod_name="pod-B", namespace="ns-2", total_cost=0.9, timestamp=TIMESTAMP_DT),
    ]
    mock_calculator.calculate_emissions.side_effect = [FAKE_CALC_RESULT_A, FAKE_CALC_RESULT_B, FAKE_CALC_RESULT_C]
    mock_calculator.pue = 1.5

    processor = DataProcessor(mock_kepler, mock_opencost, mock_calculator)
    combined_results = processor.run()

    assert len(combined_results) == 3
    assert mock_calculator.calculate_emissions.call_count == 3

    result_c = next((r for r in combined_results if r.pod_name == "pod-C"), None)
    assert result_c is not None
    # --- Use config value for default cost assertion ---
    assert result_c.total_cost == config.DEFAULT_COST
    # --------------------------------------------------
    # --- Added detailed assertions for Pod C ---
    assert result_c.namespace == "ns-1"
    assert result_c.co2e_grams == FAKE_CALC_RESULT_C.co2e_grams
    assert result_c.grid_intensity == FAKE_CALC_RESULT_C.grid_intensity
    assert result_c.pue == 1.5
    # -----------------------------------------

    result_a = next(r for r in combined_results if r.pod_name == "pod-A")
    assert result_a.total_cost == 0.3
    result_b = next(r for r in combined_results if r.pod_name == "pod-B")
    assert result_b.total_cost == 0.9

