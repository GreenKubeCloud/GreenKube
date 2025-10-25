# src/greenkube/core/processor.py
import logging
from collections import defaultdict
from ..collectors.kepler_collector import KeplerCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.pod_collector import PodCollector
from ..storage.base_repository import CarbonIntensityRepository
from ..models.metrics import CombinedMetric
from ..core.calculator import CarbonCalculator
from ..core.config import config
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone

logger = logging.getLogger(__name__)

class DataProcessor:
    """ Orchestrates data collection, processing, and calculation. """

    def __init__(self,
                 kepler_collector: KeplerCollector,
                 opencost_collector: OpenCostCollector,
                 node_collector: NodeCollector,
                 pod_collector: PodCollector,
                 repository: CarbonIntensityRepository,
                 calculator: CarbonCalculator):
        self.kepler_collector = kepler_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
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
            cost_map = {metric.pod_name: metric for metric in cost_metrics if metric.pod_name}
        except Exception as e:
            logger.error("Failed to collect data from OpenCost: %s", e)
            cost_map = {}

        # 4. Collect Pod Request Data from K8s API
        try:
            pod_metrics = self.pod_collector.collect()
            logger.info("Successfully collected %d pod request metrics.", len(pod_metrics))
            
            # Aggregate container requests up to the pod level
            pod_request_map = defaultdict(lambda: {"cpu": 0, "memory": 0})
            for pod_metric in pod_metrics:
                key = (pod_metric.namespace, pod_metric.pod_name)
                pod_request_map[key]["cpu"] += pod_metric.cpu_request
                pod_request_map[key]["memory"] += pod_metric.memory_request
            
        except Exception as e:
            logger.error("Failed to collect data from PodCollector: %s", e)
            pod_request_map = {}

        # 5. Combine and Calculate
        for energy_metric in energy_metrics:
            pod_name = energy_metric.pod_name
            namespace = energy_metric.namespace
            pod_key = (namespace, pod_name)

            # Find corresponding cost metric
            cost_metric = cost_map.get(pod_name)
            total_cost = cost_metric.total_cost if cost_metric else config.DEFAULT_COST

            # Find corresponding pod requests
            pod_requests = pod_request_map.get(pod_key, {"cpu": 0, "memory": 0})

            # Determine Electricity Maps Zone
            node_name = energy_metric.node
            cloud_zone = cloud_zones_map.get(node_name) if cloud_zones_map else None
            emaps_zone = config.DEFAULT_ZONE

            if cloud_zone:
                mapped_zone = get_emaps_zone_from_cloud_zone(cloud_zone)
                if mapped_zone:
                    emaps_zone = mapped_zone
                else:
                     logger.warning("Could not map cloud zone '%s' for node '%s'. Using default: '%s'.", cloud_zone, node_name, config.DEFAULT_ZONE)
            else:
                if node_name and cloud_zones_map:
                     logger.warning("Zone not found for node '%s'. Using default zone '%s'.", node_name, config.DEFAULT_ZONE)
                emaps_zone = config.DEFAULT_ZONE

            # Calculate Carbon Emissions
            try:
                carbon_result = self.calculator.calculate_emissions(
                    joules=energy_metric.joules,
                    zone=emaps_zone,
                    timestamp=energy_metric.timestamp
                )
            except Exception as e:
                 logger.error("Failed to calculate emissions for pod '%s': %s", pod_name, e)
                 carbon_result = None

            if carbon_result:
                combined = CombinedMetric(
                    pod_name=pod_name,
                    namespace=namespace,
                    total_cost=total_cost,
                    co2e_grams=carbon_result.co2e_grams,
                    pue=self.calculator.pue,
                    grid_intensity=carbon_result.grid_intensity,
                    joules=energy_metric.joules,
                    cpu_request=pod_requests["cpu"],
                    memory_request=pod_requests["memory"]
                )
                combined_metrics.append(combined)
            else:
                 logger.info("Skipping combined metric for pod '%s' due to calculation error.", pod_name)

        logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))
        return combined_metrics
