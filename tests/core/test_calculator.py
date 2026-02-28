# tests/core/test_calculator.py

from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculator

# --- Import the config object ---
from greenkube.core.config import config
from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE

# --------------------------------

# Define constants for tests
TEST_ZONE = "FR"
TEST_TIMESTAMP = "2023-10-27T10:00:00Z"
TEST_JOULES = config.JOULES_PER_KWH * 2  # Equivalent to 2 kWh
TEST_INTENSITY = 150.0  # gCO2e/kWh


@pytest.fixture
def mock_repository():
    """Creates a mock for the CarbonIntensityRepository."""
    mock = MagicMock()
    mock.get_for_zone_at_time = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_calculate_emissions_success(mock_repository):
    """
    Verifies the correct calculation of emissions when carbon intensity data is available.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = TEST_INTENSITY
    # --- Use config.DEFAULT_PUE for initialization ---
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    # -----------------------------------------------

    # Act
    result = await calculator.calculate_emissions(joules=TEST_JOULES, zone=TEST_ZONE, timestamp=TEST_TIMESTAMP)

    # Assert
    # 1. Verify repository call
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)

    # 2. Verify calculation
    # Energy before PUE = 2 kWh
    # Energy after PUE = 2 kWh * config.DEFAULT_PUE (e.g., 1.5) = 3 kWh
    # CO2e = 3 kWh * 150 gCO2e/kWh = 450 g
    expected_co2e = (TEST_JOULES / config.JOULES_PER_KWH) * config.DEFAULT_PUE * TEST_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e)  # Use approx for floats
    assert result.grid_intensity == TEST_INTENSITY


@pytest.mark.asyncio
async def test_calculate_emissions_no_intensity_data_zone_default(mock_repository):
    """
    Verifies that when repository returns None for a known zone (FR),
    the calculator uses the zone-specific default from the CSV table.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = None
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    zone_default_intensity = float(DEFAULT_GRID_INTENSITY_BY_ZONE["FR"])

    # Act
    result = await calculator.calculate_emissions(joules=TEST_JOULES, zone=TEST_ZONE, timestamp=TEST_TIMESTAMP)

    # Assert
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)
    expected_co2e = (TEST_JOULES / config.JOULES_PER_KWH) * config.DEFAULT_PUE * zone_default_intensity
    assert result.co2e_grams == pytest.approx(expected_co2e)
    assert result.grid_intensity == zone_default_intensity


@pytest.mark.asyncio
async def test_calculate_emissions_no_intensity_data_global_fallback(mock_repository):
    """
    Verifies that when repository returns None for an unknown zone (not in
    the default CSV), the calculator falls back to config.DEFAULT_INTENSITY.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = None
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    unknown_zone = "XX-UNKNOWN"

    # Act
    result = await calculator.calculate_emissions(joules=TEST_JOULES, zone=unknown_zone, timestamp=TEST_TIMESTAMP)

    # Assert
    expected_co2e = (TEST_JOULES / config.JOULES_PER_KWH) * config.DEFAULT_PUE * config.DEFAULT_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e)
    assert result.grid_intensity == config.DEFAULT_INTENSITY


@pytest.mark.asyncio
async def test_calculate_emissions_zero_joules(mock_repository):
    """
    Verifies that the calculation returns result based on joule consumption.
    """
    # Arrange
    mock_repository.get_for_zone_at_time.return_value = TEST_INTENSITY
    # --- Use config.DEFAULT_PUE for initialization ---
    calculator = CarbonCalculator(repository=mock_repository, pue=config.DEFAULT_PUE)
    # -----------------------------------------------

    # Act
    result = await calculator.calculate_emissions(
        joules=0.1,  # consumption
        zone=TEST_ZONE,
        timestamp=TEST_TIMESTAMP,
    )

    # Assert
    # Repository is still called to get the grid intensity
    mock_repository.get_for_zone_at_time.assert_called_once_with(TEST_ZONE, TEST_TIMESTAMP)

    # CO2e result must be calculated
    expected_co2e_zero_joules = (0.1 / config.JOULES_PER_KWH) * config.DEFAULT_PUE * TEST_INTENSITY
    assert result.co2e_grams == pytest.approx(expected_co2e_zero_joules)
    # Grid intensity is still reported
    assert result.grid_intensity == TEST_INTENSITY
