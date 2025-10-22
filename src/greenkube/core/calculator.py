# src/greenkube/core/calculator.py

from dataclasses import dataclass
from ..storage.base_repository import CarbonIntensityRepository
# --- Import the config object ---
from .config import config
# --------------------------------

@dataclass
class CarbonCalculationResult:
    """ Holds the results of a carbon emission calculation. """
    co2e_grams: float
    grid_intensity: float

class CarbonCalculator:
    """ Calculates CO2e emissions based on energy consumption and grid carbon intensity. """

    def __init__(self, repository: CarbonIntensityRepository, pue: float = config.DEFAULT_PUE):
        """
        Initializes the CarbonCalculator.

        Args:
            repository: An object that adheres to the CarbonIntensityRepository interface,
                        used to fetch grid carbon intensity data.
            pue: The Power Usage Effectiveness factor for the data center. Defaults to config.DEFAULT_PUE.
        """
        self.repository = repository
        self.pue = pue # Store PUE for use in calculations

    def calculate_emissions(self, joules: float, zone: str, timestamp: str) -> CarbonCalculationResult:
        """
        Calculates CO2 equivalent emissions for a given energy consumption.

        Args:
            joules: Energy consumed in Joules.
            zone: The electricity map zone (e.g., 'FR', 'DE').
            timestamp: The timestamp (ISO format string) when the energy consumption occurred.

        Returns:
            A CarbonCalculationResult containing the calculated CO2e in grams
            and the grid intensity value used for the calculation.
        """
        # --- Fetch intensity data using the timestamp ---
        grid_intensity_value = self.repository.get_for_zone_at_time(zone, timestamp)
        # ---------------------------------------------

        # --- Handle missing intensity data using the default from config ---
        if grid_intensity_value is None:
            print(f"WARN: Carbon intensity data not found for zone '{zone}' at {timestamp}. Using default value: {config.DEFAULT_INTENSITY} gCO2e/kWh.")
            grid_intensity_value = config.DEFAULT_INTENSITY # Use configured default
        # ------------------------------------------------------------------

        if joules == 0.0:
            # If no energy was consumed, CO2e is 0, but we still return the grid intensity
            return CarbonCalculationResult(co2e_grams=0.0, grid_intensity=grid_intensity_value)

        # Convert Joules to kWh
        kwh = joules / config.JOULES_PER_KWH

        # Apply PUE factor
        kwh_adjusted_for_pue = kwh * self.pue

        # Calculate CO2e in grams (kWh * gCO2e/kWh)
        co2e_grams = kwh_adjusted_for_pue * grid_intensity_value

        return CarbonCalculationResult(co2e_grams=co2e_grams, grid_intensity=grid_intensity_value)

