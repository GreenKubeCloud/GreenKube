# src/greenkube/core/processor.py

from ..collectors.kepler_collector import KeplerCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.node_collector import NodeCollector
from ..storage.base_repository import CarbonIntensityRepository
from ..models.metrics import CombinedMetric
from ..core.calculator import CarbonCalculator
# --- Import the config object for default values ---
from ..core.config import config
# -------------------------------------------------
# --- Correct import name for the mapping translator ---
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
# -----------------------------------------------------

class DataProcessor:
    """ Orchestrates data collection, processing, and calculation. """

    def __init__(self,
                 kepler_collector: KeplerCollector,
                 opencost_collector: OpenCostCollector,
                 node_collector: NodeCollector,
                 repository: CarbonIntensityRepository,
                 calculator: CarbonCalculator):
        self.kepler_collector = kepler_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.repository = repository
        self.calculator = calculator

    def run(self):
        """ Executes the data processing pipeline. """
        print("INFO: Starting data processing cycle...")
        combined_metrics = []

        # 1. Get Node Zones (or use default if unavailable)
        try:
            cloud_zones_map = self.node_collector.collect()
            if not cloud_zones_map:
                print(f"WARN: NodeCollector returned no zones. Using default zone '{config.DEFAULT_ZONE}' for Electricity Maps lookup.")
        except Exception as e:
            print(f"ERROR: Failed to collect node zones: {e}. Using default zone '{config.DEFAULT_ZONE}' for Electricity Maps lookup.")
            cloud_zones_map = {} # Ensure it's iterable

        # 2. Collect Energy Data from Kepler
        try:
            energy_metrics = self.kepler_collector.collect()
            print(f"INFO: Successfully collected {len(energy_metrics)} metrics from Kepler.")
        except Exception as e:
            print(f"ERROR: Failed to collect data from Kepler: {e}")
            energy_metrics = [] # Continue with empty list if Kepler fails

        # 3. Collect Cost Data from OpenCost
        try:
            cost_metrics = self.opencost_collector.collect()
            print(f"INFO: Successfully collected {len(cost_metrics)} metrics from OpenCost.")
            # Convert list to dict for efficient lookup
            cost_map = {metric.pod_name: metric for metric in cost_metrics if metric.pod_name}
        except Exception as e:
            print(f"ERROR: Failed to collect data from OpenCost: {e}")
            cost_map = {} # Continue with empty map if OpenCost fails


        # 4. Combine and Calculate
        for energy_metric in energy_metrics:
            pod_name = energy_metric.pod_name

            # Find corresponding cost metric, use default if missing
            cost_metric = cost_map.get(pod_name)
            if cost_metric:
                total_cost = cost_metric.total_cost
                cost_timestamp = cost_metric.timestamp # Use cost timestamp if available
            else:
                print(f"WARN: Cost data not found for pod '{pod_name}'. Using default cost: {config.DEFAULT_COST}.")
                total_cost = config.DEFAULT_COST
                cost_timestamp = energy_metric.timestamp # Fallback to energy timestamp

            # Determine Electricity Maps Zone based on node region or default
            node_name = energy_metric.node # Assuming Kepler provides the node name
            cloud_zone = cloud_zones_map.get(node_name) if cloud_zones_map else None

            if cloud_zone:
                emaps_zone = get_emaps_zone_from_cloud_zone(cloud_zone)
                if not emaps_zone:
                     print(f"WARN: Could not map cloud zone '{cloud_zone}' for node '{node_name}' to Electricity Maps zone. Using default: '{config.DEFAULT_ZONE}'.")
                     emaps_zone = config.DEFAULT_ZONE
            else:
                # Use default zone if node zone couldn't be determined earlier or isn't in the map
                if node_name and cloud_zones_map: # Check if map was loaded but node wasn't found
                     print(f"WARN: Zone not found for node '{node_name}'. Using default zone '{config.DEFAULT_ZONE}'.")
                # If cloud_zones_map is empty, the warning was already printed.
                emaps_zone = config.DEFAULT_ZONE

            # Calculate Carbon Emissions using the energy metric's timestamp
            try:
                carbon_result = self.calculator.calculate_emissions(
                    joules=energy_metric.joules,
                    zone=emaps_zone,
                    timestamp=energy_metric.timestamp # Pass the timestamp
                )
            except Exception as e:
                 print(f"ERROR: Failed to calculate emissions for pod '{pod_name}': {e}")
                 carbon_result = None # Handle calculation errors gracefully

            if carbon_result:
                combined = CombinedMetric(
                    pod_name=pod_name,
                    namespace=energy_metric.namespace,
                    total_cost=total_cost,
                    co2e_grams=carbon_result.co2e_grams,
                    pue=self.calculator.pue, # Get PUE from the calculator instance
                    grid_intensity=carbon_result.grid_intensity
                )
                combined_metrics.append(combined)
            else:
                 # Optionally create a metric with defaults even if calculation fails
                 print(f"INFO: Skipping combined metric for pod '{pod_name}' due to calculation error.")


        print(f"INFO: Processing complete. Found {len(combined_metrics)} combined metrics.")
        return combined_metrics

