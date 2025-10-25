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
import logging

logger = logging.getLogger(__name__)

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
        logger.info("Starting data processing cycle...")
        combined_metrics = []

        # 1. Get Node Zones (or use default if unavailable)
        try:
            cloud_zones_map = self.node_collector.collect()
            if not cloud_zones_map:
                logger.warning("NodeCollector returned no zones. Using default zone '%s' for Electricity Maps lookup.", config.DEFAULT_ZONE)
        except Exception as e:
            logger.error("Failed to collect node zones: %s. Using default zone '%s' for Electricity Maps lookup.", e, config.DEFAULT_ZONE)
            cloud_zones_map = {} # Ensure it's iterable

        # 2. Collect Energy Data from Kepler
        try:
            energy_metrics = self.kepler_collector.collect()
            logger.info("Successfully collected %d metrics from Kepler.", len(energy_metrics))
        except Exception as e:
            logger.error("Failed to collect data from Kepler: %s", e)
            energy_metrics = [] # Continue with empty list if Kepler fails

        # 3. Collect Cost Data from OpenCost
        try:
            cost_metrics = self.opencost_collector.collect()
            logger.info("Successfully collected %d metrics from OpenCost.", len(cost_metrics))
            # Convert list to dict for efficient lookup
            cost_map = {metric.pod_name: metric for metric in cost_metrics if metric.pod_name}
        except Exception as e:
            logger.error("Failed to collect data from OpenCost: %s", e)
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
                logger.warning("Cost data not found for pod '%s'. Using default cost: %s.", pod_name, config.DEFAULT_COST)
                total_cost = config.DEFAULT_COST
                cost_timestamp = energy_metric.timestamp # Fallback to energy timestamp

            # Determine Electricity Maps Zone based on node region or default
            node_name = energy_metric.node # Assuming Kepler provides the node name
            cloud_zone = cloud_zones_map.get(node_name) if cloud_zones_map else None

            if cloud_zone:
                emaps_zone = get_emaps_zone_from_cloud_zone(cloud_zone)
                if not emaps_zone:
                     logger.warning("Could not map cloud zone '%s' for node '%s' to Electricity Maps zone. Using default: '%s'.", cloud_zone, node_name, config.DEFAULT_ZONE)
                     emaps_zone = config.DEFAULT_ZONE
            else:
                # Use default zone if node zone couldn't be determined earlier or isn't in the map
                if node_name and cloud_zones_map: # Check if map was loaded but node wasn't found
                     logger.warning("Zone not found for node '%s'. Using default zone '%s'.", node_name, config.DEFAULT_ZONE)
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
                 logger.error("Failed to calculate emissions for pod '%s': %s", pod_name, e)
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
                 logger.info("Skipping combined metric for pod '%s' due to calculation error.", pod_name)


        logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))
        return combined_metrics

