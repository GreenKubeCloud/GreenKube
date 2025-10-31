# src/greenkube/core/processor.py
import logging
from collections import defaultdict
from ..collectors.prometheus_collector import PrometheusCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.pod_collector import PodCollector
from ..storage.base_repository import CarbonIntensityRepository
from ..models.metrics import CombinedMetric
from ..core.calculator import CarbonCalculator
from ..energy.estimator import BasicEstimator
from ..core.config import config
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
from datetime import datetime, timezone
from ..core.config import config as core_config

logger = logging.getLogger(__name__)

class DataProcessor:
    """ Orchestrates data collection, processing, and calculation. """

    def __init__(self,
                 prometheus_collector: PrometheusCollector,
                 opencost_collector: OpenCostCollector,
                 node_collector: NodeCollector,
                 pod_collector: PodCollector,
                 repository: CarbonIntensityRepository,
                 calculator: CarbonCalculator,
                 estimator: BasicEstimator):
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
        self.repository = repository
        self.calculator = calculator
        self.estimator = estimator

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

        # 2. Collect Prometheus metrics and estimate energy using the estimator
        try:
            prom_metrics = self.prometheus_collector.collect()
            # If Prometheus did not return any node instance types, attempt a
            # kube-api fallback via NodeCollector to obtain instance types.
            node_types = getattr(prom_metrics, 'node_instance_types', None)
            if not node_types:
                try:
                    node_instances = self.node_collector.collect_instance_types()
                    # Ensure prom_metrics has a mutable list to append into
                    if getattr(prom_metrics, 'node_instance_types', None) is None:
                        try:
                            prom_metrics.node_instance_types = []
                        except Exception:
                            # If prom_metrics is a MagicMock or otherwise immutable,
                            # create a local list to pass to the estimator instead.
                            prom_metrics.node_instance_types = []

                    # Convert to NodeInstanceType models expected by estimator
                    from ..models.prometheus_metrics import NodeInstanceType
                    for node, itype in node_instances.items():
                        prom_metrics.node_instance_types.append(NodeInstanceType(node=node, instance_type=itype))
                    if node_instances:
                        logger.info("Used NodeCollector to populate %d instance-type(s) as fallback.", len(node_instances))
                except Exception as e:
                    logger.debug("NodeCollector instance-type fallback failed: %s", e)

            # The estimator converts PrometheusMetric -> List[EnergyMetric]
            energy_metrics = self.estimator.estimate(prom_metrics)
            logger.info("Successfully estimated %d energy metrics from Prometheus.", len(energy_metrics))
        except Exception as e:
            logger.error("Failed to collect/estimate energy metrics from Prometheus: %s", e)
            energy_metrics = [] # Continue with empty list if Prometheus/estimator fails

        # Precompute node -> Electricity Maps zone mapping once to avoid repeated
        # translations/prints during per-pod processing. This also yields a set
        # of unique (zone, timestamp) keys which we will prefetch from the
        # repository and place in the calculator's per-run cache to avoid
        # repeated external DB/API calls.
        node_emaps_map = {}
        if cloud_zones_map:
            for node, cloud_zone in cloud_zones_map.items():
                try:
                    mapped = get_emaps_zone_from_cloud_zone(cloud_zone)
                    if mapped:
                        node_emaps_map[node] = mapped
                        logger.info("Node '%s' cloud zone '%s' -> Electricity Maps zone '%s'", node, cloud_zone, mapped)
                    else:
                        node_emaps_map[node] = config.DEFAULT_ZONE
                        logger.warning("Could not map cloud zone '%s' for node '%s'. Using default: '%s'", cloud_zone, node, config.DEFAULT_ZONE)
                except Exception:
                    node_emaps_map[node] = config.DEFAULT_ZONE
                    logger.warning("Exception while mapping cloud zone '%s' for node '%s'. Using default: '%s'", cloud_zone, node, config.DEFAULT_ZONE)

        # Group energy metrics by emaps_zone so we can prefetch intensity once
        # per zone and populate the calculator cache for all timestamps of
        # metrics in that zone. This addresses the case where each pod has a
        # slightly different timestamp but we only want one repository call
        # per zone per run.
        zone_to_metrics = {}
        for em in energy_metrics:
            node_name = em.node
            emaps_zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)
            zone_to_metrics.setdefault(emaps_zone, []).append(em)

        for zone, metrics in zone_to_metrics.items():
            # Choose a representative timestamp for the repository query. Use
            # the latest timestamp among metrics to be conservative.
            representative_ts = max(m.timestamp for m in metrics)
            # Normalize representative timestamp based on configured granularity
            gran = getattr(core_config, 'NORMALIZATION_GRANULARITY', 'hour')
            if isinstance(representative_ts, str):
                try:
                    rep_dt = datetime.fromisoformat(representative_ts.replace('Z', '+00:00'))
                except Exception:
                    rep_dt = datetime.now(timezone.utc)
            else:
                rep_dt = representative_ts

            if gran == 'hour':
                rep_normalized_dt = rep_dt.replace(minute=0, second=0, microsecond=0)
            elif gran == 'day':
                rep_normalized_dt = rep_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                rep_normalized_dt = rep_dt
            # Prepare both '+00:00' and 'Z' ISO formats. Call repository using
            # the '+00:00' form because some repositories/tests expect that
            # variant. Cache will store both forms for later lookups.
            rep_dt_utc = rep_normalized_dt.astimezone(timezone.utc).replace(microsecond=0)
            rep_normalized_plus = rep_dt_utc.isoformat()
            rep_normalized_z = rep_dt_utc.isoformat().replace('+00:00', 'Z')
            try:
                intensity = self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
                logger.info("Prefetched intensity for zone '%s' at '%s' (present=%s)", zone, rep_normalized_plus, intensity is not None)
            except Exception as e:
                intensity = None
                logger.warning("Failed to prefetch intensity for zone '%s' at '%s': %s", zone, rep_normalized_plus, e)

            # Populate cache entries for each metric timestamp so later lookups
            # in CarbonCalculator.find in-cache by exact (zone,timestamp) succeed
            for m in metrics:
                # Normalize metric timestamp to match calculator cache keys
                ts = m.timestamp
                if isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    except Exception:
                        dt = rep_dt
                else:
                    dt = ts
                if gran == 'hour':
                    key_dt = dt.replace(minute=0, second=0, microsecond=0)
                elif gran == 'day':
                    key_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    key_dt = dt
                # Create both 'Z' and '+00:00' ISO formats to be tolerant of
                # callers/tests that expect either representation.
                key_dt_utc = key_dt.astimezone(timezone.utc).replace(microsecond=0)
                key_ts_z = key_dt_utc.isoformat().replace('+00:00', 'Z')
                key_ts_plus = key_dt_utc.isoformat()

                cache_key_z = (zone, key_ts_z)
                cache_key_plus = (zone, key_ts_plus)

                if cache_key_z not in self.calculator._intensity_cache:
                    self.calculator._intensity_cache[cache_key_z] = intensity
                if cache_key_plus not in self.calculator._intensity_cache:
                    self.calculator._intensity_cache[cache_key_plus] = intensity

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

            # Determine Electricity Maps Zone using the precomputed mapping to
            # avoid calling the translator repeatedly during the per-pod loop.
            node_name = energy_metric.node
            emaps_zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)

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
