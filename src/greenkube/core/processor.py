# src/greenkube/core/processor.py
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from greenkube.collectors.boavizta_collector import BoaviztaCollector
from greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector
from greenkube.utils.date_utils import parse_iso_date

from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import config
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric
from ..models.node import NodeZoneContext
from ..storage.base_repository import CarbonIntensityRepository, NodeRepository
from ..storage.embodied_repository import EmbodiedRepository
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
        electricity_maps_collector: ElectricityMapsCollector,
        boavizta_collector: BoaviztaCollector,
        repository: CarbonIntensityRepository,
        node_repository: NodeRepository,
        embodied_repository: EmbodiedRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
    ):
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
        self.electricity_maps_collector = electricity_maps_collector
        self.boavizta_collector = boavizta_collector
        self.repository = repository
        self.node_repository = node_repository
        self.embodied_repository = embodied_repository
        self.calculator = calculator
        self.estimator = estimator

    async def _get_node_emaps_map(self, nodes_info: dict = None) -> Dict[str, NodeZoneContext]:
        """Collects node zones and maps them to Electricity Maps zones.

        Args:
            nodes_info: Optional dict[str, NodeInfo] from node collector

        Returns:
            A dict mapping node names to NodeZoneContext objects.
        """
        if nodes_info is None:
            try:
                nodes_info = await self.node_collector.collect()
                if not nodes_info:
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
                nodes_info = {}

        node_contexts = {}
        if nodes_info:
            for node_name, node_info in nodes_info.items():
                cloud_zone = node_info.zone
                provider = node_info.cloud_provider
                mapped = None
                reasons = []
                is_estimated = False
                if cloud_zone:
                    try:
                        mapped = get_emaps_zone_from_cloud_zone(cloud_zone, provider=provider)
                    except Exception:
                        logger.warning(
                            "Exception while mapping cloud zone '%s' for node '%s'.",
                            cloud_zone,
                            node_name,
                            exc_info=True,
                        )

                if mapped:
                    logger.info(
                        "Node '%s' cloud zone '%s' (provider: %s) -> Electricity Maps zone '%s'",
                        node_name,
                        cloud_zone,
                        provider,
                        mapped,
                    )
                else:
                    # Fallback: try to map region if zone mapping failed
                    region = node_info.region
                    if region:
                        try:
                            mapped = get_emaps_zone_from_cloud_zone(region, provider=provider)
                        except Exception:
                            logger.warning(
                                "Failed to map region '%s' (provider: %s) to Electricity Maps zone.",
                                region,
                                provider,
                                exc_info=True,
                            )

                    if mapped:
                        reasons.append(
                            f"Node '{node_name}' region '{region}' (provider: {provider}) -> "
                            f"Electricity Maps zone '{mapped}' (fallback from zone '{cloud_zone}')"
                        )
                        is_estimated = True
                        logger.info(
                            "Node '%s' region '%s' (provider: %s) -> Electricity Maps zone '%s' "
                            "(fallback from zone '%s')",
                            node_name,
                            region,
                            provider,
                            mapped,
                            cloud_zone,
                        )
                    else:
                        mapped = config.DEFAULT_ZONE
                        reasons.append(
                            f"Could not map cloud zone '{cloud_zone}' or region '{region}'. "
                            f"Used default zone '{config.DEFAULT_ZONE}'"
                        )
                        is_estimated = True
                        logger.warning(
                            "Could not map cloud zone '%s' or region '%s' for node '%s'. Using default: '%s'",
                            cloud_zone,
                            region,
                            node_name,
                            config.DEFAULT_ZONE,
                        )

                node_contexts[node_name] = NodeZoneContext(
                    node=node_name, emaps_zone=mapped, is_estimated=is_estimated, estimation_reasons=reasons
                )

        return node_contexts

    async def run(self):
        """Executes the data processing pipeline."""
        logger.info("Starting data processing cycle...")
        combined_metrics = []

        # Define internal helper tasks for parallel execution to maintain independent error handling

        async def fetch_prometheus():
            try:
                prom_metrics = await self.prometheus_collector.collect()
                node_instance_map = {}
                # If Prometheus did not return any node instance types, attempt a
                # kube-api fallback via NodeCollector to obtain instance types.
                node_types = getattr(prom_metrics, "node_instance_types", None)
                if not node_types:
                    try:
                        node_instances = await self.node_collector.collect_instance_types()
                        # Ensure prom_metrics has a mutable list to append into
                        if getattr(prom_metrics, "node_instance_types", None) is None:
                            try:
                                prom_metrics.node_instance_types = []
                            except Exception:
                                prom_metrics.node_instance_types = []

                        # Convert to NodeInstanceType models expected by estimator
                        from ..models.prometheus_metrics import NodeInstanceType

                        for node, itype in node_instances.items():
                            prom_metrics.node_instance_types.append(NodeInstanceType(node=node, instance_type=itype))
                        if node_instances:
                            node_instance_map = node_instances
                            logger.info(
                                "Used NodeCollector to populate %d instance-type(s) as fallback.",
                                len(node_instances),
                            )
                    except Exception as e:
                        logger.debug("NodeCollector instance-type fallback failed: %s", e)
                else:
                    # Populate map from prom_metrics
                    for item in node_types:
                        node_instance_map[item.node] = item.instance_type

                return prom_metrics, node_instance_map
            except Exception as e:
                logger.error("Failed to collect/estimate energy metrics from Prometheus: %s", e)
                return None, {}

        async def fetch_opencost():
            try:
                cost_metrics = await self.opencost_collector.collect()
                logger.info("Successfully collected %d metrics from OpenCost.", len(cost_metrics))
                return {metric.pod_name: metric for metric in cost_metrics if metric.pod_name}
            except Exception as e:
                logger.error("Failed to collect data from OpenCost: %s", e)
                return {}

        async def fetch_pods():
            try:
                pod_metrics = await self.pod_collector.collect()
                # Build a simple map (namespace,pod) -> requested cores
                # And aggregate requests for CombinedMetric
                req_map = {(pm.namespace, pm.pod_name): pm.cpu_request / 1000.0 for pm in pod_metrics}

                agg_map = defaultdict(lambda: {"cpu": 0, "memory": 0})
                for pm in pod_metrics:
                    key = (pm.namespace, pm.pod_name)
                    agg_map[key]["cpu"] += pm.cpu_request
                    agg_map[key]["memory"] += pm.memory_request

                return pod_metrics, req_map, agg_map
            except Exception as e:
                logger.error("Failed to collect data from PodCollector: %s", e)
                return [], {}, {}

        async def fetch_nodes():
            try:
                return await self.node_collector.collect() or {}
            except Exception:
                return {}

        # Execute in parallel
        (prom_result, opencost_result, pod_result, nodes_info) = await asyncio.gather(
            fetch_prometheus(), fetch_opencost(), fetch_pods(), fetch_nodes()
        )

        prom_metrics, proxied_node_instance_map = prom_result
        cost_map = opencost_result
        pod_metrics_list, pod_request_map_simple, pod_request_map_agg = pod_result
        pod_request_map = pod_request_map_agg

        # Determine node_instance_map logic
        # If we got it from prometheus fetch (via fallback logic), use it.
        # Otherwise try to fill it from node collector explicitly if needed (though fallback handled it).
        node_instance_map = proxied_node_instance_map

        # --- Post-Processing / Dependency Logic ---

        # 1. Adjust Node Utilization based on Pod Requests (if Prom CPU is low)
        if prom_metrics:
            try:
                # Compute node totals from prom_metrics
                node_totals = {}
                for item in prom_metrics.pod_cpu_usage:
                    node_totals.setdefault(item.node, 0.0)
                    node_totals[item.node] += item.cpu_usage_cores

                LOW_NODE_CPU_THRESHOLD = config.LOW_NODE_CPU_THRESHOLD
                if node_totals:
                    # Build mapping pod->node (from prom_metrics) and node->list(items)
                    node_to_items = {}
                    for item in prom_metrics.pod_cpu_usage:
                        node_to_items.setdefault(item.node, []).append(item)

                    for node, total_cpu in node_totals.items():
                        if total_cpu < LOW_NODE_CPU_THRESHOLD:
                            # Sum requests for pods on this node
                            total_reqs = 0.0
                            for itm in node_to_items.get(node, []):
                                total_reqs += pod_request_map_simple.get((itm.namespace, itm.pod), 0.0)

                            if total_reqs > 0:
                                for itm in node_to_items.get(node, []):
                                    req = pod_request_map_simple.get((itm.namespace, itm.pod), 0.0)
                                    if req:
                                        itm.cpu_usage_cores = req
            except Exception as e:
                logger.warning("Failed to adjust node utilization based on pod requests: %s", e, exc_info=True)

            # Estimate Energy
            try:
                energy_metrics = self.estimator.estimate(prom_metrics)
                logger.info(
                    "Successfully estimated %d energy metrics from Prometheus.",
                    len(energy_metrics),
                )
            except Exception as e:
                logger.error("Estimator failed: %s", e)
                energy_metrics = []
        else:
            energy_metrics = []

        # 2. Emaps Context
        node_contexts = await self._get_node_emaps_map(nodes_info)

        # Group energy metrics by emaps_zone so we can prefetch intensity
        zone_to_metrics = {}
        for em in energy_metrics:
            node_name = em.node
            context = node_contexts.get(node_name)
            emaps_zone = context.emaps_zone if context else config.DEFAULT_ZONE
            zone_to_metrics.setdefault(emaps_zone, []).append(em)

        # Prefetch Intensity in Parallel (optional optimization, but we can iterate for now)
        # Or keep it sequential per zone as implementation detail allows await inside loop
        # Prefetch Intensity in Parallel
        async def prefetch_zone(zone, metrics):
            # Choose a representative timestamp for the repository query. Use
            # the latest timestamp among metrics to be conservative.
            representative_ts = max(m.timestamp for m in metrics)
            # Normalize representative timestamp based on configured granularity
            gran = getattr(config, "NORMALIZATION_GRANULARITY", "hour")
            if isinstance(representative_ts, str):
                rep_dt = parse_iso_date(representative_ts)
                if not rep_dt:
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
                intensity = await self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
                if intensity is None:
                    # TICKET-007: Attempt live fetch if DB misses
                    logger.info(
                        "Intensity missing for zone %s at %s. Attempting live fetch.", zone, rep_normalized_plus
                    )
                    history = await self.electricity_maps_collector.collect(
                        zone=zone, target_datetime=rep_normalized_dt
                    )
                    if history:
                        await self.repository.save_history(history, zone)

                    # Retry fetch from DB
                    intensity = await self.repository.get_for_zone_at_time(zone, rep_normalized_plus)
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
                    dt = parse_iso_date(ts)
                    if not dt:
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

        if zone_to_metrics:
            logger.info("Prefetching intensity for %d zones in parallel...", len(zone_to_metrics))
            await asyncio.gather(*(prefetch_zone(z, m) for z, m in zone_to_metrics.items()))

        # Collect node metadata for CombinedMetric fallback
        if not node_instance_map:
            try:
                node_instance_map = await self.node_collector.collect_instance_types() or {}
            except Exception:
                node_instance_map = {}

        # Prefetch Boavizta Data
        # We need a unique set of (provider, instance_type) for lookup
        unique_nodes_for_boavizta = set()
        if nodes_info:
            for node_name, info in nodes_info.items():
                if info.cloud_provider and info.instance_type:
                    unique_nodes_for_boavizta.add((info.cloud_provider, info.instance_type))

        # Populate Cache in Parallel
        # 1. Check DB first
        boavizta_cache = {}  # Map (provider, type) -> profile_dict

        missing_in_db = []
        for provider, itype in unique_nodes_for_boavizta:
            profile = await self.embodied_repository.get_profile(provider, itype)
            if profile:
                boavizta_cache[(provider, itype)] = profile
            else:
                missing_in_db.append((provider, itype))

        # 2. Fetch missing from API
        async def fetch_and_save_boavizta(provider, instance_type):
            try:
                impact = await self.boavizta_collector.get_server_impact(
                    provider=provider, instance_type=instance_type, verbose=True
                )
                if impact and impact.impacts and impact.impacts.gwp and impact.impacts.gwp.manufacture:
                    # Parse GWP from Pydantic model
                    gwp_embedded_kg = impact.impacts.gwp.manufacture
                    if gwp_embedded_kg:
                        # Default lifespan 4 years (35040 hours) per config requirement
                        # but API doesn't return it usually
                        lifespan = 35040

                        await self.embodied_repository.save_profile(
                            provider=provider, instance_type=instance_type, gwp=gwp_embedded_kg, lifespan=lifespan
                        )
                        return (provider, instance_type), {
                            "gwp_manufacture": gwp_embedded_kg,
                            "lifespan_hours": lifespan,
                        }
            except Exception as e:
                logger.warning(f"Failed to fetch/save Boavizta profile for {provider}/{instance_type}: {e}")
            return None

        if missing_in_db:
            logger.info(f"Fetching {len(missing_in_db)} missing Boavizta profiles from API...")
            results = await asyncio.gather(*(fetch_and_save_boavizta(p, i) for p, i in missing_in_db))
            for res in results:
                if res:
                    key, val = res
                    boavizta_cache[key] = val

        # Update NodeInfo with embodied emissions and save snapshots
        if nodes_info:
            for node_name, info in nodes_info.items():
                if info.cloud_provider and info.instance_type:
                    key = (info.cloud_provider, info.instance_type)
                    profile = boavizta_cache.get(key)
                    if profile:
                        info.embodied_emissions_kg = profile.get("gwp_manufacture")

            try:
                await self.node_repository.save_nodes(list(nodes_info.values()))
                logger.info("Saved %d node snapshots.", len(nodes_info))
            except Exception as e:
                logger.warning("Failed to save node snapshots: %s", e)

        # 5. Combine and Calculate
        for energy_metric in energy_metrics:
            pod_name = energy_metric.pod_name
            namespace = energy_metric.namespace
            pod_key = (namespace, pod_name)

            # Find corresponding cost metric
            cost_metric = cost_map.get(pod_name)
            if cost_metric:
                total_cost = cost_metric.total_cost
            else:
                total_cost = config.DEFAULT_COST

            # Find corresponding pod requests
            pod_requests = pod_request_map.get(pod_key, {"cpu": 0, "memory": 0})

            # Determine Electricity Maps Zone using the precomputed mapping to
            # avoid calling the translator repeatedly during the per-pod loop.
            node_name = energy_metric.node
            node_context = node_contexts.get(node_name)
            emaps_zone = node_context.emaps_zone if node_context else config.DEFAULT_ZONE

            # Collect estimation reasons
            estimation_reasons = []
            is_estimated = False

            # 1. From Energy Estimation (Instance Profile)
            if energy_metric.is_estimated:
                is_estimated = True
                estimation_reasons.extend(energy_metric.estimation_reasons)

            # 2. From Zone Mapping
            if node_context:
                if node_context.is_estimated:
                    is_estimated = True
                    estimation_reasons.extend(node_context.estimation_reasons)
            else:
                # Fallback if node not in map at all (should be rare given _get_node_emaps_map logic)
                is_estimated = True
                estimation_reasons.append(
                    f"Node '{node_name}' not found in zone map. Used default zone '{config.DEFAULT_ZONE}'"
                )

            # 3. From Cost
            if not cost_metric:
                is_estimated = True
                estimation_reasons.append(f"No cost data for pod '{pod_name}'. Used default cost {config.DEFAULT_COST}")

            # 4. From PUE
            provider = nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
            pue = config.get_pue_for_provider(provider)
            # Check if we used a default PUE because provider was unknown or missing
            # config.get_pue_for_provider returns DEFAULT_PUE if provider is None or unknown
            # We can check if provider is None or if it's not in the profiles
            if not provider:
                is_estimated = True
                estimation_reasons.append(f"Unknown provider for node '{node_name}'. Used default PUE {pue}")
            elif f"default_{provider.lower()}" not in config.DATACENTER_PUE_PROFILES:
                # This check mimics get_pue_for_provider logic to detect fallback
                is_estimated = True
                estimation_reasons.append(f"No PUE profile for provider '{provider}'. Used default PUE {pue}")

            # 5. Embodied Emissions (Boavizta)
            embodied_emissions_grams = 0.0
            node_info = nodes_info.get(node_name)
            if node_info and node_info.cloud_provider and node_info.instance_type:
                key = (node_info.cloud_provider, node_info.instance_type)
                profile = boavizta_cache.get(key)

                if profile:
                    try:
                        gwp_kg = profile.get("gwp_manufacture")
                        lifespan = profile.get("lifespan_hours")

                        # Calculate share
                        node_capacity = 0
                        if node_info.instance_type:
                            prof = self.estimator.instance_profiles.get(node_info.instance_type)
                            if prof:
                                node_capacity = prof["vcores"]

                        cpu_share = 0.0
                        if node_capacity > 0 and pod_requests["cpu"] > 0:
                            cpu_share = (pod_requests["cpu"] / 1000.0) / node_capacity
                            cpu_share = min(cpu_share, 1.0)

                        duration = self.estimator.query_range_step_sec

                        embodied_emissions_grams = self.calculator.calculate_embodied_emissions(
                            gwp_manufacture_kg=gwp_kg,
                            lifespan_hours=lifespan,
                            duration_seconds=duration,
                            share=cpu_share,
                        )
                    except Exception as e:
                        logger.warning(f"Error calculating embodied emissions for {node_name}: {e}")

            # Calculate Carbon Emissions
            try:
                carbon_result = await self.calculator.calculate_emissions(
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
                    pue=config.get_pue_for_provider(
                        nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
                    ),
                    grid_intensity=carbon_result.grid_intensity,
                    joules=energy_metric.joules,
                    cpu_request=pod_requests["cpu"],
                    memory_request=pod_requests["memory"],
                    timestamp=energy_metric.timestamp,
                    grid_intensity_timestamp=carbon_result.grid_intensity_timestamp,
                    node=node_name,
                    node_instance_type=(
                        nodes_info.get(node_name).instance_type
                        if nodes_info.get(node_name)
                        else node_instance_map.get(node_name)
                    ),
                    node_zone=nodes_info.get(node_name).zone if nodes_info.get(node_name) else None,
                    emaps_zone=emaps_zone,
                    is_estimated=is_estimated,
                    estimation_reasons=estimation_reasons,
                    embodied_co2e_grams=embodied_emissions_grams,
                )
                combined_metrics.append(combined)
            else:
                logger.info(
                    "Skipping combined metric for pod '%s' due to calculation error.",
                    pod_name,
                )

        logger.info("Processing complete. Found %d combined metrics.", len(combined_metrics))
        self.calculator.clear_cache()
        return combined_metrics

    async def close(self):
        """Close all collectors to release resources."""
        await self.prometheus_collector.close()
        await self.opencost_collector.close()
        await self.node_collector.close()
        await self.pod_collector.close()
        await self.electricity_maps_collector.close()
        await self.boavizta_collector.close()
        logger.debug("DataProcessor closed all collectors.")

    async def run_range(
        self,
        start,
        end,
        step=None,
        namespace=None,
    ) -> List[CombinedMetric]:
        """Generate CombinedMetric list for a historical time range.

        This method centralizes the logic previously located in the CLI. It
        expects naive or aware datetimes (we treat them as UTC) and returns
        a list of CombinedMetric objects for the requested range. Optional
        parameters mirror the CLI behavior (namespace filter, monthly/yearly
        aggregation).
        """
        # Try to read from repository first to use historical metadata if available
        try:
            # Ensure start/end are datetime objects
            if isinstance(start, str):
                start_dt = parse_iso_date(start)
            else:
                start_dt = start

            if isinstance(end, str):
                end_dt = parse_iso_date(end)
            else:
                end_dt = end

            if start_dt and end_dt:
                stored_metrics = await self.repository.read_combined_metrics(start_dt, end_dt)
                if stored_metrics:
                    logger.info(
                        "Found %d stored metrics in repository for range %s - %s",
                        len(stored_metrics),
                        start,
                        end,
                    )
                    if namespace:
                        stored_metrics = [m for m in stored_metrics if m.namespace == namespace]
                    return stored_metrics
        except Exception as e:
            logger.warning("Failed to read stored metrics: %s", e)

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
            results = await self.prometheus_collector.collect_range(
                start=start, end=end, step=chosen_step, query=primary_query
            )
        except Exception:
            # If collector raises, fall back to empty results to continue pipeline
            logger.warning(
                "Prometheus collector failed to return range results; attempting fallback query via collector."
            )
            try:
                fallback_query = f"sum(rate(container_cpu_usage_seconds_total[{rate_window}])) by (namespace,pod,node)"
                results = await self.prometheus_collector.collect_range(
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
                    logger.debug("Skipping invalid metric value: ts=%s, val=%s", ts_val, val)
                    continue
                key = (series_ns, pod)
                samples[ts_f][key] += usage
                pod_node_map_by_ts[ts_f][key] = node

        # Prepare processor components
        estimator = self.estimator
        calculator = self.calculator
        repository = self.repository
        node_repository = self.node_repository
        node_collector = self.node_collector
        pod_collector = self.pod_collector

        # --- Historical Node Data Logic ---
        # 1. Fetch initial state (latest snapshot before start)
        initial_snapshots = await node_repository.get_latest_snapshots_before(start_dt)
        # 2. Fetch changes during the interval
        snapshot_changes = await node_repository.get_snapshots(start_dt, end_dt)

        # Build a timeline of node configurations
        # node_timeline[node_name] = [(timestamp, NodeInfo), ...]
        node_timeline = defaultdict(list)

        # Populate initial state
        for node_info in initial_snapshots:
            # Use node_info.timestamp if available, otherwise fallback to start_dt
            # This ensures we track the actual age of the snapshot
            ts = node_info.timestamp if node_info.timestamp else start_dt
            node_timeline[node_info.name].append((ts, node_info))

        # Add changes
        for ts_str, node_info in snapshot_changes:
            # Ensure timestamp is datetime
            if isinstance(ts_str, str):
                change_dt = parse_iso_date(ts_str)
            else:
                change_dt = ts_str

            if change_dt:
                node_timeline[node_info.name].append((change_dt, node_info))

        # Sort timelines
        for node_name in node_timeline:
            node_timeline[node_name].sort(key=lambda x: x[0])

        def get_node_info_at(node_name: str, timestamp: datetime):
            """Finds the active NodeInfo for a node at a specific time."""
            timeline = node_timeline.get(node_name)
            if not timeline:
                return None

            # Find the latest snapshot <= timestamp
            # Since timeline is sorted, we can iterate backwards or use bisect
            # Given the small number of changes per node usually, linear scan backwards is fine
            for ts, info in reversed(timeline):
                if ts <= timestamp:
                    # Check for staleness
                    age = timestamp - ts
                    if age > timedelta(days=config.NODE_DATA_MAX_AGE_DAYS):
                        logger.warning(
                            "Node snapshot for '%s' at %s is too old (age: %s). Ignoring.",
                            node_name,
                            ts,
                            age,
                        )
                        return None
                    return info
            return None

        # Fallback to current state if no history
        try:
            current_node_map = await node_collector.collect_instance_types() or {}
        except Exception:
            current_node_map = {}

        def profile_for_node(node_name: str, timestamp: datetime):
            # Try historical data first
            node_info = get_node_info_at(node_name, timestamp)
            inst = None
            cpu_capacity = None

            if node_info:
                inst = node_info.instance_type
                cpu_capacity = node_info.cpu_capacity_cores
            else:
                # Fallback to current state
                inst = current_node_map.get(node_name)

            if inst:
                profile = estimator.instance_profiles.get(inst)
                if profile:
                    return profile

                # Fallback logic for unknown instance types or "cpu-X" types
                if isinstance(inst, str) and inst.startswith("cpu-"):
                    try:
                        cores = int(inst.split("-", 1)[1])
                        return estimator._create_cpu_profile(cores)
                    except Exception:
                        logger.debug("Failed to parse inferred CPU count from instance type '%s'", inst)

            # If we have cpu_capacity from snapshot but no known instance type profile,
            # we can try to estimate based on capacity
            if cpu_capacity:
                return estimator._create_cpu_profile(cpu_capacity)

            return estimator.DEFAULT_INSTANCE_PROFILE

        # pod request maps
        try:
            pod_metrics_list = await pod_collector.collect()
            # Aggregate by (namespace, pod_name)
            pod_request_map_agg = defaultdict(int)
            pod_mem_map_agg = defaultdict(int)
            for p in pod_metrics_list:
                key = (p.namespace, p.pod_name)
                pod_request_map_agg[key] += p.cpu_request
                pod_mem_map_agg[key] += p.memory_request
            pod_request_map = pod_request_map_agg
            pod_mem_map = pod_mem_map_agg
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
                profile = profile_for_node(node_name, sample_dt)
                calculated_metrics = estimator.calculate_node_energy(
                    node_name=node_name,
                    node_profile=profile,
                    node_total_cpu=node_total_cpu.get(node_name, 0.0),
                    pods_on_node=pods_on_node,
                    duration_seconds=chosen_step_sec,
                )

                for m in calculated_metrics:
                    m.timestamp = sample_dt
                    all_energy_metrics.append(m)

        # Prefetch intensities per zone/hour and populate calculator cache
        node_contexts = await self._get_node_emaps_map()

        zone_to_metrics = defaultdict(list)
        skipped_carbon = 0
        for em in all_energy_metrics:
            node_name = em.node
            context = node_contexts.get(node_name)
            zone = context.emaps_zone if context else config.DEFAULT_ZONE
            zone_to_metrics[zone].append(em)

        for zone, metrics in zone_to_metrics.items():
            for m in metrics:
                ts = m.timestamp
                key_dt = ts.replace(minute=0, second=0, microsecond=0)
                key_dt_utc = key_dt.astimezone(timezone.utc).replace(microsecond=0)
                key_plus = key_dt_utc.isoformat()
                key_z = key_plus.replace("+00:00", "Z")
                cache_key_plus = (zone, key_plus)
                cache_key_z = (zone, key_z)
                if cache_key_plus not in calculator._intensity_cache and cache_key_z not in calculator._intensity_cache:
                    try:
                        intensity = await repository.get_for_zone_at_time(zone, key_plus)
                    except Exception:
                        intensity = None
                    calculator._intensity_cache[cache_key_plus] = intensity
                    calculator._intensity_cache[cache_key_z] = intensity

        # now build CombinedMetric list using calculator
        combined: List[CombinedMetric] = []
        try:
            # Use collect_range to get costs aggregated over the requested period
            cost_metrics = await self.opencost_collector.collect_range(start=start, end=end)
            cost_map = {c.pod_name: c for c in cost_metrics}
        except Exception:
            cost_map = {}

        # Get cloud zones and instance types for metadata
        try:
            nodes_info = await self.node_collector.collect() or {}
        except Exception:
            nodes_info = {}

        for em in all_energy_metrics:
            pod_name = em.pod_name
            em_namespace = em.namespace
            node_name = em.node
            joules = em.joules
            ts = em.timestamp
            node_context = node_contexts.get(node_name)
            zone = node_context.emaps_zone if node_context else config.DEFAULT_ZONE
            try:
                carbon_result = await calculator.calculate_emissions(joules=joules, zone=zone, timestamp=ts)
            except Exception:
                carbon_result = None
            if carbon_result is None:
                skipped_carbon += 1

            cost_metric = cost_map.get(pod_name)
            if cost_metric:
                total_cost = cost_metric.total_cost
            else:
                total_cost = config.DEFAULT_COST

            # Collect estimation reasons
            estimation_reasons = []
            is_estimated = False

            # 1. From Energy Estimation (Instance Profile)
            # EnergyMetric from estimator is a dict here because of calculate_node_energy return type in run_range
            # Wait, calculate_node_energy returns dicts, but run_range appends them to all_energy_metrics
            # So em is a dict.
            # 1. From Energy Estimation (Instance Profile)
            if em.is_estimated:
                is_estimated = True
                estimation_reasons.extend(em.estimation_reasons)

            # 2. From Zone Mapping
            if node_context:
                if node_context.is_estimated:
                    is_estimated = True
                    estimation_reasons.extend(node_context.estimation_reasons)
            else:
                is_estimated = True
                estimation_reasons.append(
                    f"Node '{node_name}' not found in zone map. Used default zone '{config.DEFAULT_ZONE}'"
                )

            # 3. From Cost
            if not cost_metric:
                is_estimated = True
                estimation_reasons.append(f"No cost data for pod '{pod_name}'. Used default cost {config.DEFAULT_COST}")

            # 4. From PUE
            provider = nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
            pue = config.get_pue_for_provider(provider)
            if not provider:
                is_estimated = True
                estimation_reasons.append(f"Unknown provider for node '{node_name}'. Used default PUE {pue}")
            elif f"default_{provider.lower()}" not in config.DATACENTER_PUE_PROFILES:
                is_estimated = True
                estimation_reasons.append(f"No PUE profile for provider '{provider}'. Used default PUE {pue}")

            cpu_req = pod_request_map.get((em_namespace, pod_name), 0)
            mem_req = pod_mem_map.get((em_namespace, pod_name), 0)
            if carbon_result:
                combined.append(
                    CombinedMetric(
                        pod_name=pod_name,
                        namespace=em_namespace,
                        period=None,
                        total_cost=total_cost,
                        timestamp=ts,
                        duration_seconds=chosen_step_sec,
                        grid_intensity_timestamp=carbon_result.grid_intensity_timestamp,
                        co2e_grams=carbon_result.co2e_grams,
                        pue=config.get_pue_for_provider(
                            nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
                        ),
                        grid_intensity=carbon_result.grid_intensity,
                        joules=joules,
                        cpu_request=cpu_req,
                        memory_request=mem_req,
                        node=node_name,
                        node_instance_type=(
                            nodes_info.get(node_name).instance_type
                            if nodes_info.get(node_name)
                            else current_node_map.get(node_name)
                        ),
                        node_zone=nodes_info.get(node_name).zone if nodes_info.get(node_name) else None,
                        emaps_zone=zone,
                        is_estimated=is_estimated,
                        estimation_reasons=estimation_reasons,
                    )
                )

        if namespace:
            combined = [c for c in combined if c.namespace == namespace]

        self.calculator.clear_cache()
        return combined
