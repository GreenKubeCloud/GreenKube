# tests/core/test_calculator.py

import pytest
from unittest.mock import MagicMock
from src.greenkube.core.calculator import CarbonCalculator, CarbonCalculationResult
# --- Import the config object ---
from src.greenkube.core.config import config
# --------------------------------

# Define constants for tests
TEST_ZONE = 'FR'
TEST_TIMESTAMP = "2023-10-27T10:00:00Z"
TEST_JOULES = config.JOULES_PER_KWH * 2 # Equivalent to 2 kWh
TEST_INTENSITY = 150.0 # gCO2e/kWh

@pytest.fixture
def mock_repository():
    """ Creates a mock for the CarbonIntensityRepository. """
    return MagicMock()

def test_calculate_emissions_success(mock_repository):
    """
    Verifies the correct calculation of emissions when carbon intensity data is available.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = TEST_INTENSITY
    # --- Use config.DEFAULT_PUE for initialization ---
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    # -----------------------------------------------

    # Act
    result = calculator.calculate_emissions(
        joules=TEST_JOULES,
        zone=TEST_ZONE,
        timestamp=TEST_TIMESTAMP
    )

    # Assert
    # 1. Verify repository call
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)

    # 2. Verify calculation
    # Energy before PUE = 2 kWh
    # Energy after PUE = 2 kWh * config.DEFAULT_PUE (e.g., 1.5) = 3 kWh
    # CO2e = 3 kWh * 150 gCO2e/kWh = 450 g
    expected_co2e = (TEST_JOULES / config.JOULES_PER_KWH) * config.DEFAULT_PUE * TEST_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e) # Use approx for floats
    assert result.grid_intensity == TEST_INTENSITY

def test_calculate_emissions_no_intensity_data(mock_repository):
    """
    Verifies that the calculation applies the formula using the configured
    DEFAULT_INTENSITY when carbon intensity data is unavailable.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = None # Simulate missing data
    # --- Use config.DEFAULT_PUE for initialization ---
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    # -----------------------------------------------
    # --- Removed the hardcoded effective_default_intensity variable ---

    # Act
    result = calculator.calculate_emissions(
        joules=TEST_JOULES,
        zone=TEST_ZONE,
        timestamp=TEST_TIMESTAMP
    )

    # Assert
    # 1. Verify repository call
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)

    # 2. Verify calculation using config.DEFAULT_INTENSITY
    # --- Use config.DEFAULT_INTENSITY in the expected calculation ---
    expected_co2e_with_default = (TEST_JOULES / config.JOULES_PER_KWH) * config.DEFAULT_PUE * config.DEFAULT_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e_with_default)
    # --- Check against config.DEFAULT_INTENSITY ---
    assert result.grid_intensity == config.DEFAULT_INTENSITY
    # -------------------------------------------

def test_calculate_emissions_zero_joules(mock_repository):
    """
    Verifies that the calculation returns 0 CO2e when joule consumption is 0,
    but still reports the available grid intensity.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = TEST_INTENSITY
    # --- Use config.DEFAULT_PUE for initialization ---
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    # -----------------------------------------------

    # Act
    result = calculator.calculate_emissions(
        joules=0.1, # Zero consumption
        zone=TEST_ZONE,
        timestamp=TEST_TIMESTAMP
    )

    # Assert
    # Repository is still called to get the grid intensity
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)

    # CO2e result must be 0 (as per formula with 0 joules)
    expected_co2e_zero_joules = (0.1 / config.JOULES_PER_KWH) * config.DEFAULT_PUE * TEST_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e_zero_joules)
    # Grid intensity is still reported
    assert result.grid_intensity == TEST_INTENSITY

