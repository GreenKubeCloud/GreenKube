# src/greenkube/core/processor.py
import logging
from collections import defaultdict
from datetime import datetime, timezone

from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import config
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone

logger = logging.getLogger(__name__)


class DataProcessor:
    """Orchestrates data collection, processing, and calculation."""

    def __init__(
        self,
        prometheus_collector: PrometheusCollector,
        opencost_collector: OpenCostCollector,
        node_collector: NodeCollector,
        pod_collector: PodCollector,
        repository: CarbonIntensityRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
    ):
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
        self.repository = repository
        self.calculator = calculator
        self.estimator = estimator

    def run(self):
        """Executes the data processing pipeline."""
        logger.info("Starting data processing cycle...")
        combined_metrics = []

        # 1. Get Node Zones (or use default if unavailable)
        try:
            cloud_zones_map = self.node_collector.collect()
            if not cloud_zones_map:
                logger.warning(
                    "NodeCollector returned no zones. Using default zone '%s' for Electricity Maps lookup.",
                    config.DEFAULT_ZONE,
                )
        except Exception as e:
            logger.error(
                "Failed to collect node zones: %s. Using default zone '%s' for Electricity Maps lookup.",
                e,
                config.DEFAULT_ZONE,
            )
            cloud_zones_map = {}  # Ensure it's iterable

        # 2. Collect Prometheus metrics
        try:
            prom_metrics = self.prometheus_collector.collect()
            # If Prometheus did not return any node instance types, attempt a
            # kube-api fallback via NodeCollector to obtain instance types.
            node_types = getattr(prom_metrics, "node_instance_types", None)
            if not node_types:
                try:
                    node_instances = self.node_collector.collect_instance_types()
                    # Ensure prom_metrics has a mutable list to append into
                    if getattr(prom_metrics, "node_instance_types", None) is None:
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
                        logger.info(
                            "Used NodeCollector to populate %d instance-type(s) as fallback.",
                            len(node_instances),
                        )
                except Exception as e:
                    logger.debug("NodeCollector instance-type fallback failed: %s", e)

            # Collect pod requests early so we can use them as a fallback when
            # Prometheus reports extremely low node CPU usage (which can make
            # energy attribution unstable).
            try:
                pod_metrics = self.pod_collector.collect()
                # Build a simple map (namespace,pod) -> requested cores
                pod_request_map = {(pm.namespace, pm.pod_name): pm.cpu_request / 1000.0 for pm in pod_metrics}
            except Exception:
                pod_request_map = {}

            # If Prometheus reports very small total CPU per node, replace or
            # augment per-pod cpu_usage_cores with the pod request value to
            # avoid giving every pod the node's minWatts energy.
            try:
                # Compute node totals from prom_metrics
                node_totals = {}
                for item in prom_metrics.pod_cpu_usage:
                    node_totals.setdefault(item.node, 0.0)
                    node_totals[item.node] += item.cpu_usage_cores

                # Threshold in cores below which Prometheus totals are considered too small
                LOW_NODE_CPU_THRESHOLD = 0.05  # 50 millicores
                if node_totals:
                    # Build mapping pod->node (from prom_metrics) and node->list(items)
                    pod_to_items = {}
                    node_to_items = {}
                    for item in prom_metrics.pod_cpu_usage:
                        pod_key = (item.namespace, item.pod)
                        pod_to_items[pod_key] = item
                        node_to_items.setdefault(item.node, []).append(item)

                    for node, total_cpu in node_totals.items():
                        if total_cpu < LOW_NODE_CPU_THRESHOLD:
                            # Sum requests for pods on this node
                            total_reqs = 0.0
                            for itm in node_to_items.get(node, []):
                                total_reqs += pod_request_map.get((itm.namespace, itm.pod), 0.0)

                            if total_reqs > 0:
                                # Replace per-pod usage with request cores to compute
                                # node utilization from declared requests rather than
                                # noisy Prometheus usage.
                                for itm in node_to_items.get(node, []):
                                    req = pod_request_map.get((itm.namespace, itm.pod), 0.0)
                                    if req:
                                        itm.cpu_usage_cores = req
            except Exception:
                # If anything goes wrong, proceed with original prom_metrics
                pass

            # The estimator converts PrometheusMetric -> List[EnergyMetric]
            energy_metrics = self.estimator.estimate(prom_metrics)
            logger.info(
                "Successfully estimated %d energy metrics from Prometheus.",
                len(energy_metrics),
            )
        except Exception as e:
            logger.error("Failed to collect/estimate energy metrics from Prometheus: %s", e)
            energy_metrics = []  # Continue with empty list if Prometheus/estimator fails

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
                        logger.info(
                            "Node '%s' cloud zone '%s' -> Electricity Maps zone '%s'",
                            node,
                            cloud_zone,
                            mapped,
                        )
                    else:
                        node_emaps_map[node] = config.DEFAULT_ZONE
                        logger.warning(
                            "Could not map cloud zone '%s' for node '%s'. Using default: '%s'",
                            cloud_zone,
                            node,
                            config.DEFAULT_ZONE,
                        )
                except Exception:
                    node_emaps_map[node] = config.DEFAULT_ZONE
                    logger.warning(
                        "Exception while mapping cloud zone '%s' for node '%s'. Using default: '%s'",
                        cloud_zone,
                        node,
                        config.DEFAULT_ZONE,
                    )

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
            gran = getattr(config, "NORMALIZATION_GRANULARITY", "hour")
            if isinstance(representative_ts, str):
                try:
                    rep_dt = datetime.fromisoformat(representative_ts.replace("Z", "+00:00"))
                except Exception:
                    rep_dt = datetime.now(timezone.utc)
            else:
                rep_dt = representative_ts

            if gran == "hour":
                rep_normalized_dt = rep_dt.replace(minute=0, second=0, microsecond=0)
            elif gran == "day":
                rep_normalized_dt = rep_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                rep_normalized_dt = rep_dt
            # Prepare both '+00:00' and 'Z' ISO formats. Call repository using
            # the '+00:00' form because some repositories/tests expect that
            # variant. Cache will store both forms for later lookups.
            rep_dt_utc = rep_normalized_dt.astimezone(timezone.utc).replace(microsecond=0)
            rep_normalized_plus = rep_dt_utc.isoformat()
            try:
                intensity = self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
                logger.info(
                    "Prefetched intensity for zone '%s' at '%s' (present=%s)",
                    zone,
                    rep_normalized_plus,
                    intensity is not None,
                )
            except Exception as e:
                intensity = None
                logger.warning(
                    "Failed to prefetch intensity for zone '%s' at '%s': %s",
                    zone,
                    rep_normalized_plus,
                    e,
                )

            # Populate cache entries for each metric timestamp so later lookups
            # in CarbonCalculator.find in-cache by exact (zone,timestamp) succeed
            for m in metrics:
                # Normalize metric timestamp to match calculator cache keys
                ts = m.timestamp
                if isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        dt = rep_dt
                else:
                    dt = ts
                if gran == "hour":
                    key_dt = dt.replace(minute=0, second=0, microsecond=0)
                elif gran == "day":
                    key_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    key_dt = dt
                # Create both 'Z' and '+00:00' ISO formats to be tolerant of
                # callers/tests that expect either representation.
                key_dt_utc = key_dt.astimezone(timezone.utc).replace(microsecond=0)
                key_ts_z = key_dt_utc.isoformat().replace("+00:00", "Z")
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
                    timestamp=energy_metric.timestamp,
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
                    memory_request=pod_requests["memory"],
                )
                combined_metrics.append(combined)
            else:
                logger.info(
                    "Skipping combined metric for pod '%s' due to calculation error.",
                    pod_name,
                )

        logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))
        return combined_metrics

    def run_range(
        self,
        start,
        end,
        step=None,
        namespace=None,
    ):
        """Generate CombinedMetric list for a historical time range.

        This method centralizes the logic previously located in the CLI. It
        expects naive or aware datetimes (we treat them as UTC) and returns
        a list of CombinedMetric objects for the requested range. Optional
        parameters mirror the CLI behavior (namespace filter, monthly/yearly
        aggregation).
        """

        # Helper to format ISO with Z
        def iso_z(dt):
            return dt.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        # Determine safe step if not provided
        def parse_duration_to_seconds(s: str) -> int:
            s = str(s).strip()
            try:
                if s.endswith("s"):
                    return int(s[:-1])
                if s.endswith("m"):
                    return int(s[:-1]) * 60
                if s.endswith("h"):
                    return int(s[:-1]) * 3600
                return int(s)
            except Exception:
                return 60

        cfg_step_str = config.PROMETHEUS_QUERY_RANGE_STEP
        cfg_step_sec = parse_duration_to_seconds(cfg_step_str)
        # Use the configured step directly. The logic to increase it was causing fewer data points for long ranges.
        chosen_step_sec = cfg_step_sec
        chosen_step = f"{chosen_step_sec}s"

        # Use the Prometheus collector to fetch range-series
        # The rate window should match the step to avoid gaps/overlaps.
        rate_window = f"{chosen_step_sec}s"
        primary_query = f"sum(rate(container_cpu_usage_seconds_total[{rate_window}])) by (namespace,pod,container,node)"
        try:
            results = self.prometheus_collector.collect_range(
                start=start, end=end, step=chosen_step, query=primary_query
            )
        except Exception:
            # If collector raises, fall back to empty results to continue pipeline
            logger.warning(
                "Prometheus collector failed to return range results; attempting fallback query via collector."
            )
            try:
                fallback_query = f"sum(rate(container_cpu_usage_seconds_total[{rate_window}])) by (namespace,pod,node)"
                results = self.prometheus_collector.collect_range(
                    start=start, end=end, step=chosen_step, query=fallback_query
                )
            except Exception:
                results = []

        # parse results into samples
        samples = defaultdict(lambda: defaultdict(float))
        pod_node_map_by_ts = defaultdict(dict)
        for series in results:
            metric = series.get("metric", {}) or {}
            series_ns = metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("namespace_name")
            pod = (
                metric.get("pod")
                or metric.get("pod_name")
                or metric.get("kubernetes_pod_name")
                or metric.get("container")
            )
            node = metric.get("node") or metric.get("kubernetes_node") or ""
            if not series_ns or not pod:
                continue
            for ts_val, val in series.get("values", []):
                try:
                    usage = float(val)
                    ts_f = float(ts_val)
                except Exception:
                    continue
                key = (series_ns, pod)
                samples[ts_f][key] += usage
                pod_node_map_by_ts[ts_f][key] = node

        # Prepare processor components
        estimator = self.estimator
        calculator = self.calculator
        repository = self.repository
        node_collector = self.node_collector
        pod_collector = self.pod_collector

        # node instance types
        try:
            node_instance_map = node_collector.collect_instance_types() or {}
        except Exception:
            node_instance_map = {}

        def profile_for_node(node_name: str):
            inst = node_instance_map.get(node_name)
            if inst:
                profile = estimator.instance_profiles.get(inst)
                if profile:
                    return profile
                if isinstance(inst, str) and inst.startswith("cpu-"):
                    try:
                        cores = int(inst.split("-", 1)[1])
                        default_vcores = estimator.DEFAULT_INSTANCE_PROFILE["vcores"]
                        if default_vcores <= 0:
                            per_core_min = estimator.DEFAULT_INSTANCE_PROFILE["minWatts"]
                            per_core_max = estimator.DEFAULT_INSTANCE_PROFILE["maxWatts"]
                        else:
                            per_core_min = estimator.DEFAULT_INSTANCE_PROFILE["minWatts"] / default_vcores
                            per_core_max = estimator.DEFAULT_INSTANCE_PROFILE["maxWatts"] / default_vcores
                        return {
                            "vcores": cores,
                            "minWatts": per_core_min * cores,
                            "maxWatts": per_core_max * cores,
                        }
                    except Exception:
                        pass
            return estimator.DEFAULT_INSTANCE_PROFILE

        # pod request maps
        try:
            pod_metrics_list = pod_collector.collect()
            pod_request_map = {(p.namespace, p.pod_name): p.cpu_request for p in pod_metrics_list}
            pod_mem_map = {(p.namespace, p.pod_name): p.memory_request for p in pod_metrics_list}
        except Exception:
            pod_request_map = {}
            pod_mem_map = {}

        all_energy_metrics = []

        # Normalize samples by the chosen step to avoid floating point timestamp issues
        normalized_samples = defaultdict(lambda: defaultdict(float))
        for ts_f, pod_map in samples.items():
            normalized_ts_f = (ts_f // chosen_step_sec) * chosen_step_sec
            for pod_key, cpu_usage in pod_map.items():
                normalized_samples[normalized_ts_f][pod_key] += cpu_usage

        for ts_f, pod_map in sorted(normalized_samples.items()):
            sample_dt = datetime.fromtimestamp(ts_f, tz=timezone.utc)

            pod_cpu_usage = pod_map
            node_total_cpu = defaultdict(float)
            node_pod_map = defaultdict(list)
            for pod_key, cpu in pod_cpu_usage.items():
                node = pod_node_map_by_ts.get(ts_f, {}).get(pod_key) or ""
                node_total_cpu[node] += cpu
                node_pod_map[node].append((pod_key, cpu))

            for node_name, pods_on_node in node_pod_map.items():
                profile = profile_for_node(node_name)
                vcores = profile.get("vcores", 1)
                min_watts = profile.get("minWatts", 1.0)
                max_watts = profile.get("maxWatts", 1.0)
                total_cpu = node_total_cpu.get(node_name, 0.0)
                node_util = (total_cpu / vcores) if vcores > 0 else 0.0
                node_util = min(node_util, 1.0)
                node_power_watts = min_watts + (node_util * (max_watts - min_watts))

                if total_cpu <= 0:
                    for pod_key, cpu_cores in pods_on_node:
                        em_namespace, pod = pod_key
                        num_pods_on_node = len(pods_on_node)
                        power_draw_watts = (min_watts / num_pods_on_node) if num_pods_on_node > 0 else 0
                        joules = power_draw_watts * chosen_step_sec
                        em = {
                            "pod_name": pod,
                            "namespace": em_namespace,
                            "joules": joules,
                            "node": node_name,
                            "timestamp": sample_dt,
                        }
                        all_energy_metrics.append(em)
                else:
                    for pod_key, cpu_cores in pods_on_node:
                        em_namespace, pod = pod_key
                        share = cpu_cores / total_cpu if total_cpu > 0 else 0.0
                        pod_power = node_power_watts * share
                        joules = pod_power * chosen_step_sec
                        em = {
                            "pod_name": pod,
                            "namespace": em_namespace,
                            "joules": joules,
                            "node": node_name,
                            "timestamp": sample_dt,
                        }
                        all_energy_metrics.append(em)

        # Prefetch intensities per zone/hour and populate calculator cache
        try:
            cloud_zones_map = node_collector.collect() or {}
        except Exception:
            cloud_zones_map = {}

        node_emaps_map = {}
        for node, cz in cloud_zones_map.items():
            emz = get_emaps_zone_from_cloud_zone(cz) or config.DEFAULT_ZONE
            node_emaps_map[node] = emz

        zone_to_metrics = defaultdict(list)
        skipped_carbon = 0
        for em in all_energy_metrics:
            node_name = em["node"]
            zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)
            zone_to_metrics[zone].append(em)

        for zone, metrics in zone_to_metrics.items():
            for m in metrics:
                ts = m["timestamp"]
                key_dt = ts.replace(minute=0, second=0, microsecond=0)
                key_dt_utc = key_dt.astimezone(timezone.utc).replace(microsecond=0)
                key_plus = key_dt_utc.isoformat()
                key_z = key_plus.replace("+00:00", "Z")
                cache_key_plus = (zone, key_plus)
                cache_key_z = (zone, key_z)
                if cache_key_plus not in calculator._intensity_cache and cache_key_z not in calculator._intensity_cache:
                    try:
                        intensity = repository.get_for_zone_at_time(zone, key_plus)
                    except Exception:
                        intensity = None
                    calculator._intensity_cache[cache_key_plus] = intensity
                    calculator._intensity_cache[cache_key_z] = intensity

        # now build CombinedMetric list using calculator
        combined = []
        try:
            cost_metrics = self.opencost_collector.collect()
            cost_map = {c.pod_name: c for c in cost_metrics}
        except Exception:
            cost_map = {}

        for em in all_energy_metrics:
            pod_name = em["pod_name"]
            em_namespace = em["namespace"]
            node_name = em["node"]
            joules = em["joules"]
            ts = em["timestamp"]
            zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)
            try:
                carbon_result = calculator.calculate_emissions(joules=joules, zone=zone, timestamp=ts)
            except Exception:
                carbon_result = None
            if carbon_result is None:
                skipped_carbon += 1

            total_cost = cost_map.get(pod_name).total_cost if cost_map.get(pod_name) else config.DEFAULT_COST
            cpu_req = pod_request_map.get((em_namespace, pod_name), 0)
            mem_req = pod_mem_map.get((em_namespace, pod_name), 0)
            if carbon_result:
                combined.append(
                    {
                        "pod_name": pod_name,
                        "namespace": em_namespace,
                        "period": None,
                        "total_cost": total_cost,
                        "timestamp": ts,
                        "duration_seconds": chosen_step_sec,
                        "grid_intensity_timestamp": carbon_result.grid_intensity_timestamp,
                        "co2e_grams": carbon_result.co2e_grams,
                        "pue": calculator.pue,
                        "grid_intensity": carbon_result.grid_intensity,
                        "joules": joules,
                        "cpu_request": cpu_req,
                        "memory_request": mem_req,
                    }
                )

        # optional namespace filter
        if namespace:
            combined = [c for c in combined if c.namespace == namespace]

        return combined
