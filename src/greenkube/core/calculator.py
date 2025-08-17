# src/greenkube/core/calculator.py
"""
The brain of GreenKube. This module contains the core logic for transforming
raw energy metrics into meaningful carbon emission data.
"""
from typing import List, Dict
from ..models.metrics import EnergyMetric, CarbonEmissionMetric, EnvironmentalMetric

# Conversion factor from Joules to kilowatt-hours (kWh)
JOULES_PER_KWH = 3_600_000

class CarbonCalculator:
    """
    A class responsible for calculating carbon emissions from energy metrics.
    It no longer holds state (PUE, grid intensity).
    """
    def calculate_carbon_emissions(
        self,
        energy_metrics: List[EnergyMetric],
        environmental_data: Dict[str, EnvironmentalMetric]
    ) -> List[CarbonEmissionMetric]:
        """
        Calculates CO2e emissions for a list of energy consumption metrics using
        region-specific environmental data.

        The formula is:
        Energy (kWh) = Energy (Joules) / 3,600,000
        Total Energy (kWh) = Energy (kWh) * PUE
        CO2e (grams) = Total Energy (kWh) * Grid Intensity (gCO2e/kWh)

        Args:
            energy_metrics: A list of EnergyMetric objects from a collector.
            environmental_data: A dictionary mapping a region (str) to its
                                EnvironmentalMetric (PUE and grid intensity).

        Returns:
            A list of CarbonEmissionMetric objects with the calculated emissions.
        """
        carbon_emissions = []
        for metric in energy_metrics:
            if not metric.region or metric.region not in environmental_data:
                # Skip calculation if we don't have environmental data for the region
                continue

            env_metric = environmental_data[metric.region]
            
            energy_kwh = metric.joules / JOULES_PER_KWH
            total_energy_kwh = energy_kwh * env_metric.pue
            co2e_grams = total_energy_kwh * env_metric.grid_intensity

            carbon_emissions.append(
                CarbonEmissionMetric(
                    pod_name=metric.pod_name,
                    namespace=metric.namespace,
                    co2e_grams=co2e_grams
                )
            )
        return carbon_emissions