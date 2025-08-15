# src/greenkube/core/calculator.py
"""
The brain of GreenKube. This module contains the core logic for transforming
raw energy metrics into meaningful carbon emission data.
"""
from typing import List
from ..models.metrics import EnergyMetric, CarbonEmissionMetric

# Conversion factor from Joules to kilowatt-hours (kWh)
JOULES_PER_KWH = 3_600_000

class CarbonCalculator:
    """
    A class responsible for calculating carbon emissions from energy metrics.
    """
    def __init__(self, pue: float = 1.5, grid_intensity_gco2e_per_kwh: float = 50.0):
        """
        Initializes the calculator with key environmental factors.

        Args:
            pue: Power Usage Effectiveness of the data center. A measure of
                 how much extra energy is used for cooling, etc.
            grid_intensity_gco2e_per_kwh: The carbon intensity of the electrical
                 grid in grams of CO2 equivalent per kWh. Default is a low
                 value representing a green grid (like in France).
        """
        if pue < 1.0:
            raise ValueError("PUE must be greater than or equal to 1.0")
        self.pue = pue
        self.grid_intensity = grid_intensity_gco2e_per_kwh

    def calculate_carbon_emissions(self, energy_metrics: List[EnergyMetric]) -> List[CarbonEmissionMetric]:
        """
        Calculates CO2e emissions for a list of energy consumption metrics.

        The formula is:
        Energy (kWh) = Energy (Joules) / 3,600,000
        Total Energy (kWh) = Energy (kWh) * PUE
        CO2e (grams) = Total Energy (kWh) * Grid Intensity (gCO2e/kWh)

        Args:
            energy_metrics: A list of EnergyMetric objects from a collector.

        Returns:
            A list of CarbonEmissionMetric objects with the calculated emissions.
        """
        carbon_emissions = []
        for metric in energy_metrics:
            energy_kwh = metric.joules / JOULES_PER_KWH
            total_energy_kwh = energy_kwh * self.pue
            co2e_grams = total_energy_kwh * self.grid_intensity

            carbon_emissions.append(
                CarbonEmissionMetric(
                    pod_name=metric.pod_name,
                    namespace=metric.namespace,
                    co2e_grams=co2e_grams
                )
            )
        return carbon_emissions