# src/greenkube/core/historical_range_processor.py
"""Processes historical time ranges by querying Prometheus range API."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from .. import __version__
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..core.calculator import CarbonCalculator
from ..core.config import Config, get_config
from ..core.metric_assembler import MetricAssembler
from ..core.node_zone_mapper import NodeZoneMapper
from ..energy.estimator import BasicEstimator
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository, CombinedMetricsRepository, NodeRepository
from ..utils.date_utils import parse_iso_date

logger = logging.getLogger(__name__)


class HistoricalRangeProcessor:
    """Generate CombinedMetric lists for historical time ranges.

    Processes the range in day-sized chunks to limit peak memory usage.
    """

    def __init__(
        self,
        prometheus_collector: PrometheusCollector,
        opencost_collector: OpenCostCollector,
        node_collector: NodeCollector,
        pod_collector: PodCollector,
        repository: CarbonIntensityRepository,
        combined_metrics_repository: CombinedMetricsRepository,
        node_repository: NodeRepository,
        calculator: CarbonCalculator,
        estimator: BasicEstimator,
        assembler: MetricAssembler,
        zone_mapper: NodeZoneMapper,
        config: Config | None = None,
    ):
        self.prometheus_collector = prometheus_collector
        self.opencost_collector = opencost_collector
        self.node_collector = node_collector
        self.pod_collector = pod_collector
        self.repository = repository
        self.combined_metrics_repository = combined_metrics_repository
        self.node_repository = node_repository
        self.calculator = calculator
        self.estimator = estimator
        self.assembler = assembler
        self.zone_mapper = zone_mapper
        self._config = config if config is not None else get_config()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_duration_to_seconds(s: str) -> int:
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_range(
        self,
        start,
        end,
        step=None,
        namespace=None,
    ) -> List[CombinedMetric]:
        """Generate CombinedMetric list for a historical time range.

        To avoid OOM for large clusters over long periods, the range is
        processed in day-sized chunks.
        """
        # Try to read from repository first
        try:
            if isinstance(start, str):
                start_dt = parse_iso_date(start)
            else:
                start_dt = start

            if isinstance(end, str):
                end_dt = parse_iso_date(end)
            else:
                end_dt = end

            if start_dt and end_dt:
                stored_metrics = await self.combined_metrics_repository.read_combined_metrics(start_dt, end_dt)
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

        cfg_step_str = self._config.PROMETHEUS_QUERY_RANGE_STEP
        cfg_step_sec = self._parse_duration_to_seconds(cfg_step_str)
        chosen_step_sec = cfg_step_sec
        chosen_step = f"{chosen_step_sec}s"
        rate_window = f"{chosen_step_sec}s"

        estimator = self.estimator
        calculator = self.calculator
        repository = self.repository
        node_repository = self.node_repository
        node_collector = self.node_collector
        pod_collector = self.pod_collector

        # Historical Node Data
        initial_snapshots = await node_repository.get_latest_snapshots_before(start_dt)
        snapshot_changes = await node_repository.get_snapshots(start_dt, end_dt)

        node_timeline = defaultdict(list)
        for node_info in initial_snapshots:
            ts = node_info.timestamp if node_info.timestamp else start_dt
            node_timeline[node_info.name].append((ts, node_info))
        for ts_str, node_info in snapshot_changes:
            if isinstance(ts_str, str):
                change_dt = parse_iso_date(ts_str)
            else:
                change_dt = ts_str
            if change_dt:
                node_timeline[node_info.name].append((change_dt, node_info))
        for node_name in node_timeline:
            node_timeline[node_name].sort(key=lambda x: x[0])

        def get_node_info_at(node_name: str, timestamp: datetime):
            timeline = node_timeline.get(node_name)
            if not timeline:
                return None
            for ts, info in reversed(timeline):
                if ts <= timestamp:
                    age = timestamp - ts
                    if age > timedelta(days=self._config.NODE_DATA_MAX_AGE_DAYS):
                        logger.warning(
                            "Node snapshot for '%s' at %s is too old (age: %s). Ignoring.",
                            node_name,
                            ts,
                            age,
                        )
                        return None
                    return info
            return None

        try:
            current_node_map = await node_collector.collect_instance_types() or {}
        except Exception:
            current_node_map = {}

        def profile_for_node(node_name: str, timestamp: datetime):
            node_info = get_node_info_at(node_name, timestamp)
            inst = None
            cpu_capacity = None
            if node_info:
                inst = node_info.instance_type
                cpu_capacity = node_info.cpu_capacity_cores
            else:
                inst = current_node_map.get(node_name)
            if inst:
                profile = estimator.instance_profiles.get(inst)
                if profile:
                    return profile
                if isinstance(inst, str) and inst.startswith("cpu-"):
                    try:
                        cores = int(inst.split("-", 1)[1])
                        return estimator._create_cpu_profile(cores)
                    except Exception:
                        logger.debug(
                            "Failed to parse inferred CPU count from instance type '%s'",
                            inst,
                        )
            if cpu_capacity:
                return estimator._create_cpu_profile(cpu_capacity)
            return estimator.DEFAULT_INSTANCE_PROFILE

        # Pod request maps
        try:
            pod_metrics_list = await pod_collector.collect()
            pod_request_map_agg = defaultdict(int)
            pod_mem_map_agg = defaultdict(int)
            pod_ephemeral_storage_map_agg = defaultdict(int)
            for p in pod_metrics_list:
                key = (p.namespace, p.pod_name)
                pod_request_map_agg[key] += p.cpu_request
                pod_mem_map_agg[key] += p.memory_request
                pod_ephemeral_storage_map_agg[key] += p.ephemeral_storage_request
            pod_request_map = pod_request_map_agg
            pod_mem_map = pod_mem_map_agg
            pod_ephemeral_storage_map = pod_ephemeral_storage_map_agg
        except Exception:
            pod_request_map = {}
            pod_mem_map = {}
            pod_ephemeral_storage_map = {}

        # Current node metadata — collected once here for zone mapping and
        # assembly.  Historical snapshots (node_timeline) cover the past;
        # this provides a best-effort fallback for nodes not yet in the DB.
        try:
            nodes_info = await self.node_collector.collect() or {}
        except Exception:
            nodes_info = {}

        # Node contexts for zone mapping — pass already-collected nodes_info
        # so the zone mapper never triggers an extra K8s API call.
        node_contexts = await self.zone_mapper.map_nodes(nodes_info)

        # Fetch / cache Boavizta embodied-emissions profiles once for the whole range.
        try:
            boavizta_cache = await self.assembler.embodied_service.prepare_embodied_data(nodes_info)
        except Exception as e:
            logger.warning("Failed to prepare embodied data: %s. Embodied emissions will be 0.", e)
            boavizta_cache = {}

        # Cost data
        range_seconds = (end_dt - start_dt).total_seconds()
        steps_in_range = max(range_seconds / chosen_step_sec, 1)
        try:
            cost_metrics = await self.opencost_collector.collect_range(start=start, end=end)
            cost_map = {c.pod_name: c for c in cost_metrics}
        except Exception:
            cost_map = {}

        # --- Chunked processing ---
        CHUNK_SIZE = timedelta(days=1)
        combined: List[CombinedMetric] = []
        chunk_start = start_dt
        skipped_carbon = 0

        while chunk_start < end_dt:
            chunk_end = min(chunk_start + CHUNK_SIZE, end_dt)
            logger.debug("Processing chunk: %s -> %s", chunk_start, chunk_end)

            primary_query = (
                f"sum(rate(container_cpu_usage_seconds_total[{rate_window}])) by (namespace,pod,container,node)"
            )
            try:
                results = await self.prometheus_collector.collect_range(
                    start=chunk_start, end=chunk_end, step=chosen_step, query=primary_query
                )
            except Exception:
                logger.warning(
                    "Prometheus collector failed for chunk %s -> %s; attempting fallback.",
                    chunk_start,
                    chunk_end,
                )
                try:
                    fallback_query = (
                        f"sum(rate(container_cpu_usage_seconds_total[{rate_window}])) by (namespace,pod,node)"
                    )
                    results = await self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=fallback_query
                    )
                except Exception:
                    results = []

            # Fetch additional resource metrics for this chunk (best-effort)
            net_rx_query = f"sum(rate(container_network_receive_bytes_total[{rate_window}])) by (namespace,pod,node)"
            net_tx_query = f"sum(rate(container_network_transmit_bytes_total[{rate_window}])) by (namespace,pod,node)"
            disk_read_query = f"sum(rate(container_fs_reads_bytes_total[{rate_window}])) by (namespace,pod,node)"
            disk_write_query = f"sum(rate(container_fs_writes_bytes_total[{rate_window}])) by (namespace,pod,node)"
            restart_query = "sum(kube_pod_container_status_restarts_total) by (namespace,pod)"
            memory_query = "sum(container_memory_working_set_bytes) by (namespace,pod,node)"

            try:
                (
                    net_rx_results,
                    net_tx_results,
                    disk_read_results,
                    disk_write_results,
                    restart_results,
                    memory_results,
                ) = await asyncio.gather(
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=net_rx_query
                    ),
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=net_tx_query
                    ),
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=disk_read_query
                    ),
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=disk_write_query
                    ),
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=restart_query
                    ),
                    self.prometheus_collector.collect_range(
                        start=chunk_start, end=chunk_end, step=chosen_step, query=memory_query
                    ),
                )
            except Exception:
                net_rx_results = []
                net_tx_results = []
                disk_read_results = []
                disk_write_results = []
                restart_results = []
                memory_results = []

            def _build_pod_map_from_range(range_results):
                pod_map = {}
                for series in range_results:
                    m = series.get("metric", {}) or {}
                    ns = m.get("namespace") or m.get("kubernetes_namespace") or ""
                    p = m.get("pod") or m.get("pod_name") or ""
                    if not ns or not p:
                        continue
                    values = series.get("values", [])
                    if values:
                        try:
                            pod_map[(ns, p)] = float(values[-1][1])
                        except (ValueError, IndexError):
                            pass
                return pod_map

            range_net_rx_map = _build_pod_map_from_range(net_rx_results)
            range_net_tx_map = _build_pod_map_from_range(net_tx_results)
            range_disk_read_map = _build_pod_map_from_range(disk_read_results)
            range_disk_write_map = _build_pod_map_from_range(disk_write_results)
            range_restart_map = _build_pod_map_from_range(restart_results)
            range_memory_map = _build_pod_map_from_range(memory_results)

            del net_rx_results, net_tx_results, disk_read_results, disk_write_results, restart_results
            del memory_results

            # Parse results into samples
            samples = defaultdict(lambda: defaultdict(float))
            pod_node_map_by_ts = defaultdict(dict)
            for series in results:
                metric = series.get("metric", {}) or {}
                series_ns = (
                    metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("namespace_name")
                )
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

            del results

            # Compute energy metrics for this chunk
            chunk_energy_metrics = []

            normalized_samples = defaultdict(lambda: defaultdict(float))
            for ts_f, pod_map in samples.items():
                normalized_ts_f = (ts_f // chosen_step_sec) * chosen_step_sec
                for pod_key, cpu_usage in pod_map.items():
                    normalized_samples[normalized_ts_f][pod_key] += cpu_usage

            # Build per-pod CPU usage map (average cores → millicores) across the chunk
            cpu_usage_sum: dict = defaultdict(float)
            cpu_usage_count: dict = defaultdict(int)
            for ts_f, pod_map in normalized_samples.items():
                for pod_key, cpu_cores in pod_map.items():
                    cpu_usage_sum[pod_key] += cpu_cores
                    cpu_usage_count[pod_key] += 1
            range_cpu_usage_map = {
                k: int(round((cpu_usage_sum[k] / cpu_usage_count[k]) * 1000))
                for k in cpu_usage_sum
                if cpu_usage_count[k] > 0
            }

            for ts_f, pod_map in sorted(normalized_samples.items()):
                sample_dt = datetime.fromtimestamp(ts_f, tz=timezone.utc)

                node_total_cpu = defaultdict(float)
                node_pod_map = defaultdict(list)
                for pod_key, cpu in pod_map.items():
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
                        chunk_energy_metrics.append(m)

            del samples, pod_node_map_by_ts, normalized_samples

            # Prefetch carbon intensities
            for em in chunk_energy_metrics:
                node_name = em.node
                context = node_contexts.get(node_name)
                zone = context.emaps_zone if context else self._config.DEFAULT_ZONE
                ts = em.timestamp
                try:
                    intensity = await repository.get_for_zone_at_time(zone, ts.isoformat())
                except Exception:
                    intensity = None
                if intensity is not None:
                    await calculator.prefetch_intensity(zone, ts.isoformat(), intensity)

            # Build CombinedMetric objects for this chunk
            for em in chunk_energy_metrics:
                pod_name = em.pod_name
                em_namespace = em.namespace
                node_name = em.node
                joules = em.joules
                ts = em.timestamp
                node_context = node_contexts.get(node_name)
                zone = node_context.emaps_zone if node_context else self._config.DEFAULT_ZONE
                provider = nodes_info.get(node_name).cloud_provider if nodes_info.get(node_name) else None
                pue = self._config.get_pue_for_provider(provider)
                try:
                    carbon_result = await calculator.calculate_emissions(
                        joules=joules, zone=zone, timestamp=ts, pue=pue
                    )
                except Exception:
                    carbon_result = None
                if carbon_result is None:
                    skipped_carbon += 1

                cost_metric = cost_map.get(pod_name)
                total_cost = cost_metric.total_cost / steps_in_range if cost_metric else self._config.DEFAULT_COST

                # Reuse the shared estimation-flags builder
                is_estimated, estimation_reasons = self.assembler.build_estimation_flags(
                    energy_metric=em,
                    node_context=node_context,
                    cost_metric=cost_metric,
                    provider=provider,
                    pue=pue,
                    node_name=node_name,
                    cpu_adjusted_nodes=set(),
                )

                cpu_req = pod_request_map.get((em_namespace, pod_name), 0)
                mem_req = pod_mem_map.get((em_namespace, pod_name), 0)
                pod_key = (em_namespace, pod_name)
                node_info_at_ts = get_node_info_at(node_name, ts)
                embodied_emissions_grams = self.assembler.embodied_service.calculate_pod_embodied(
                    node_info=node_info_at_ts or nodes_info.get(node_name),
                    boavizta_cache=boavizta_cache,
                    pod_requests={"cpu": cpu_req, "memory": mem_req},
                    cpu_usage_millicores=range_cpu_usage_map.get(pod_key),
                )
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
                            pue=pue,
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
                            node_zone=(nodes_info.get(node_name).zone if nodes_info.get(node_name) else None),
                            emaps_zone=zone,
                            is_estimated=is_estimated,
                            estimation_reasons=estimation_reasons,
                            embodied_co2e_grams=embodied_emissions_grams,
                            cpu_usage_millicores=range_cpu_usage_map.get(pod_key),
                            memory_usage_bytes=(
                                int(range_memory_map[pod_key]) if pod_key in range_memory_map else None
                            ),
                            network_receive_bytes=range_net_rx_map.get(pod_key),
                            network_transmit_bytes=range_net_tx_map.get(pod_key),
                            disk_read_bytes=range_disk_read_map.get(pod_key),
                            disk_write_bytes=range_disk_write_map.get(pod_key),
                            ephemeral_storage_request_bytes=(pod_ephemeral_storage_map.get(pod_key) or None),
                            restart_count=(int(range_restart_map[pod_key]) if pod_key in range_restart_map else None),
                            calculation_version=__version__,
                        )
                    )

            del chunk_energy_metrics
            del range_net_rx_map, range_net_tx_map
            del range_disk_read_map, range_disk_write_map, range_restart_map
            del range_cpu_usage_map, range_memory_map

            chunk_start = chunk_end

        if namespace:
            combined = [c for c in combined if c.namespace == namespace]

        await self.calculator.clear_cache()
        return combined
