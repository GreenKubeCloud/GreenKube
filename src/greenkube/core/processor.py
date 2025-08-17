# src/greenkube/core/processor.py
"""
This module acts as the orchestrator, tying together the collectors,
calculator, and reporters to execute the main application logic.
"""
from typing import List, Dict
from ..collectors.base_collector import BaseCollector
from ..core.calculator import CarbonCalculator
from ..models.metrics import CombinedMetric, EnvironmentalMetric, EnergyMetric

class DataProcessor:
    """
    Orchestrates the data collection and processing pipeline.
    """
    def __init__(self, energy_collector: BaseCollector, cost_collector: BaseCollector, calculator: CarbonCalculator):
        self.energy_collector = energy_collector
        self.cost_collector = cost_collector
        self.calculator = calculator

    def run(self) -> List[CombinedMetric]:
        """
        Executes the full data processing pipeline.
        1. Collects energy, cost, and environmental data.
        2. Calculates carbon emissions from energy data using environmental context.
        3. Combines cost and carbon data into a unified report.
        """
        # In the future, this data will come from new dedicated collectors.
        # For now, we mock it here to simulate a multi-region environment.
        environmental_data: Dict[str, EnvironmentalMetric] = {
            "us-east-1": EnvironmentalMetric(pue=1.6, grid_intensity=450.0),
            "eu-west-1": EnvironmentalMetric(pue=1.2, grid_intensity=50.0)
        }

        energy_data = self.energy_collector.collect()
        cost_data = self.cost_collector.collect()
        carbon_data = self.calculator.calculate_carbon_emissions(energy_data, environmental_data)

        # Create lookup maps for easy data access
        cost_map: Dict[str, float] = {metric.pod_name: metric.total_cost for metric in cost_data}
        carbon_map: Dict[str, float] = {metric.pod_name: metric.co2e_grams for metric in carbon_data}
        # Create a new map to look up the original energy metric for a pod
        energy_map: Dict[str, EnergyMetric] = {metric.pod_name: metric for metric in energy_data}

        combined_metrics: List[CombinedMetric] = []
        all_pod_names = set(cost_map.keys()) | set(carbon_map.keys())

        for pod_name in sorted(list(all_pod_names)):
            namespace = next((m.namespace for m in cost_data + carbon_data if m.pod_name == pod_name), "unknown")
            
            # Find the original energy metric to get the region
            original_energy_metric = energy_map.get(pod_name)
            pue = 0.0
            grid_intensity = 0.0
            if original_energy_metric and original_energy_metric.region in environmental_data:
                env_metric = environmental_data[original_energy_metric.region]
                pue = env_metric.pue
                grid_intensity = env_metric.grid_intensity

            combined_metrics.append(
                CombinedMetric(
                    pod_name=pod_name,
                    namespace=namespace,
                    total_cost=cost_map.get(pod_name, 0.0),
                    co2e_grams=carbon_map.get(pod_name, 0.0),
                    pue=pue,
                    grid_intensity=grid_intensity
                )
            )
        
        return combined_metrics
