# src/greenkube/core/historical_range_processor.py
"""Processes historical time ranges by querying Prometheus range API."""

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import List

from .. import __version__
from ..collectors.metrics_server_collector import MetricsServerCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..collectors.promql import PromQLCatalog
from ..core import collection_telemetry
from ..core.calculator import CarbonCalculator
from ..core.collection import CollectionWindow
from ..core.config import Config, get_config
from ..core.metric_assembler import MetricAssembler
from ..core.node_zone_mapper import NodeZoneMapper
from ..core.prometheus_resource_mapper import PodResourceMaps
from ..energy.estimator import BasicEstimator
from ..energy.node_power import NodePowerProfile, resolve_node_power_profile
from ..models.metrics import CombinedMetric
from ..storage.base_repository import CarbonIntensityRepository, CombinedMetricsRepository, NodeRepository
from ..utils.time import ensure_utc, parse_iso_date, parse_query_step_seconds

logger = logging.getLogger(__name__)

#: Bumped when the range pipeline changes how stored values are computed
#: (A1: pod cgroup no longer double counted; A2: half-open per-step samples;
#: A10: capacity-derived power profiles).  Rows written before carry the bare
#: package version and over-state CPU, memory and energy.
CALCULATION_VERSION = f"{__version__}+range.2"
# Day-sized chunks bound the memory of long backfills.
_CHUNK_SECONDS = 86400
# Prometheus evaluation timestamps are exact multiples of the step; tolerate float noise.
_TIMESTAMP_TOLERANCE = 1e-3


class RangeCollectionError(RuntimeError):
    """Prometheus could not be queried for a window; the window must be retried."""


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
        metrics_server_collector: MetricsServerCollector | None = None,
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
        self._metrics_server_collector = metrics_server_collector or MetricsServerCollector()
        self._promql = PromQLCatalog.from_config(self._config)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_duration_to_seconds(s: str) -> int:
        return parse_query_step_seconds(s)

    @staticmethod
    def _sample_value(raw) -> float | None:
        """Parse one Prometheus sample value, dropping non-finite and clamping negatives (A6)."""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            collection_telemetry.record_dropped_sample(collection_telemetry.DROP_REASON_MALFORMED)
            return None
        if not math.isfinite(value):
            collection_telemetry.record_dropped_sample(collection_telemetry.DROP_REASON_NON_FINITE)
            return None
        if value < 0:
            collection_telemetry.record_dropped_sample(collection_telemetry.DROP_REASON_NEGATIVE)
            return 0.0
        return value

    @classmethod
    def _iter_window_samples(cls, series: dict, chunk: CollectionWindow) -> Iterator[tuple[int, float]]:
        """Yield ``(sample_start, value)`` for the samples of ``series`` that fall in ``chunk``.

        Prometheus evaluates ``rate(x[w])`` at ``t`` over the preceding step, so
        the sample evaluated at ``t`` describes ``[t - step, t)`` and is stored
        at ``t - step`` (half-open, A2).
        """
        step = chunk.step_seconds
        start = chunk.start.timestamp()
        end = chunk.end.timestamp()
        for raw_ts, raw_value in series.get("values", []) or []:
            try:
                evaluated_at = float(raw_ts)
            except (TypeError, ValueError):
                collection_telemetry.record_dropped_sample(collection_telemetry.DROP_REASON_MALFORMED)
                continue
            sample_start = math.ceil((evaluated_at - _TIMESTAMP_TOLERANCE) / step) * step - step
            if not start <= sample_start < end:
                collection_telemetry.record_dropped_sample(collection_telemetry.DROP_REASON_OUT_OF_WINDOW)
                continue
            value = cls._sample_value(raw_value)
            if value is not None:
                yield int(sample_start), value

    @staticmethod
    def _series_identity(series: dict) -> tuple[str, str, str, str]:
        metric = series.get("metric", {}) or {}
        namespace = metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("namespace_name") or ""
        pod = metric.get("pod") or metric.get("pod_name") or metric.get("kubernetes_pod_name") or ""
        container = metric.get("container") or metric.get("container_name") or ""
        node = metric.get("node") or metric.get("kubernetes_node") or ""
        return namespace, pod, container, node

    @classmethod
    def _pod_series_by_sample(cls, results, chunk: CollectionWindow) -> dict[int, dict[tuple[str, str], float]]:
        """Sum series per pod for every sample of the chunk."""
        by_sample: dict[int, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
        for series in results or []:
            namespace, pod, _container, _node = cls._series_identity(series)
            if not namespace or not pod:
                continue
            for sample_start, value in cls._iter_window_samples(series, chunk):
                by_sample[sample_start][(namespace, pod)] += value
        return by_sample

    @classmethod
    def _container_series_by_sample(
        cls, results, chunk: CollectionWindow
    ) -> tuple[dict[int, dict[tuple[str, str], float]], dict[int, dict[tuple[str, str, str], float]]]:
        """Return pod sums and per-container values for every sample of the chunk."""
        pods: dict[int, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
        containers: dict[int, dict[tuple[str, str, str], float]] = defaultdict(lambda: defaultdict(float))
        for series in results or []:
            namespace, pod, container, _node = cls._series_identity(series)
            if not namespace or not pod:
                continue
            for sample_start, value in cls._iter_window_samples(series, chunk):
                pods[sample_start][(namespace, pod)] += value
                if container:
                    containers[sample_start][(namespace, pod, container)] += value
        return pods, containers

    async def _query_range(self, chunk: CollectionWindow, query: str):
        return await self.prometheus_collector.collect_range(
            start=chunk.first_evaluation,
            end=chunk.last_evaluation,
            step=self._promql.step,
            query=query,
        )

    async def _collect_cpu(self, chunk: CollectionWindow):
        """Container CPU series; falls back to the pod cgroup series (counted once)."""
        try:
            results = await self._query_range(chunk, self._promql.container_cpu_usage())
            if results:
                return results
        except Exception as error:
            logger.warning(
                "Container CPU range query failed for %s -> %s (%s); trying the pod cgroup series.",
                chunk.start,
                chunk.end,
                error,
            )
        try:
            return await self._query_range(chunk, self._promql.pod_cgroup_cpu_usage())
        except Exception as error:
            raise RangeCollectionError(
                f"Prometheus CPU usage is unavailable for {chunk.start.isoformat()} -> {chunk.end.isoformat()}"
            ) from error

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
        """Collect CombinedMetric rows for every step of the half-open range ``[start, end)``.

        ``start``/``end`` are widened to the configured step; each returned
        pod row has ``timestamp = T`` and ``duration_seconds = step`` and
        describes usage during ``[T, T + step)``.  Nothing is read back from
        storage: the caller decides which windows to (re)collect and writes
        the rows with an idempotent upsert.  Long ranges are processed in
        day-sized chunks to bound memory.

        Raises :class:`RangeCollectionError` when Prometheus cannot be
        queried, so callers never mistake an outage for an empty window.
        """
        start_dt = ensure_utc(start) if not isinstance(start, str) else parse_iso_date(start)
        end_dt = ensure_utc(end) if not isinstance(end, str) else parse_iso_date(end)
        if start_dt is None:
            raise ValueError(f"Could not parse start time: {start!r}")
        if end_dt is None:
            raise ValueError(f"Could not parse end time: {end!r}")
        if end_dt <= start_dt:
            raise ValueError("Historical range end must be after start")

        chosen_step_sec = self._promql.step_seconds
        window = CollectionWindow.covering(start_dt, end_dt, timedelta(seconds=chosen_step_sec))
        # Every downstream consumer (OpenCost window, cost per step) uses the aligned window.
        start = start_dt = window.start
        end = end_dt = window.end
        now = datetime.now(timezone.utc)
        current_window = end_dt >= now - timedelta(seconds=max(chosen_step_sec * 2, 60))
        # PodMetrics is point-in-time data; never use today's sample to backfill older ranges.
        container_usages = (
            await self._metrics_server_collector.collect()
            if current_window and self._config.KUBERNETES_METRICS_API_ENABLED
            else []
        )

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
                change_dt = ensure_utc(ts_str)
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
            pod_metrics_list = []
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

        def profile_for_node(node_name: str, timestamp: datetime) -> NodePowerProfile:
            """Resolve the node power profile with provenance (A10)."""
            node_info = get_node_info_at(node_name, timestamp) if node_name else None
            current_info = nodes_info.get(node_name) if node_name else None
            info = node_info or current_info
            instance_type = (info.instance_type if info else None) or current_node_map.get(node_name)
            cpu_capacity = None
            for candidate in (node_info, current_info):
                if candidate is not None and candidate.cpu_capacity_cores:
                    cpu_capacity = candidate.cpu_capacity_cores
                    break
            return resolve_node_power_profile(
                node_name=node_name,
                instance_type=instance_type,
                cpu_capacity_cores=cpu_capacity,
                provider=(info.cloud_provider if info else None),
                instance_profiles=estimator.instance_profiles,
                default_profile=estimator.DEFAULT_INSTANCE_PROFILE,
            )

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

        # Per-pod persistent storage (PVC) request/usage — best-effort.
        try:
            pod_storage_map = await self.prometheus_collector.collect_pod_storage()
        except Exception as e:
            logger.warning("Failed to collect pod storage metrics: %s", e)
            pod_storage_map = {}

        # --- Chunked processing ---
        combined: List[CombinedMetric] = []
        skipped_carbon = 0
        unknown_power_nodes: set[str] = set()
        # (node, sample_start) -> PowerSource value; seam for per-row power provenance (A11).
        node_power_sources: dict[tuple[str, int], str] = {}
        promql = self._promql

        for chunk in window.split(max(1, _CHUNK_SECONDS // chosen_step_sec)):
            chunk_start, chunk_end = chunk.start, chunk.end
            logger.debug("Processing chunk: %s -> %s", chunk_start, chunk_end)

            results = await self._collect_cpu(chunk)
            # Every query of the chunk must succeed: a Prometheus error fails the
            # window so the watermark is not advanced over missing data.
            try:
                (
                    net_rx_results,
                    net_tx_results,
                    disk_read_results,
                    disk_write_results,
                    restart_results,
                    memory_results,
                    ephemeral_usage_results,
                    pod_node_results,
                ) = await asyncio.gather(
                    self._query_range(chunk, promql.pod_network_receive()),
                    self._query_range(chunk, promql.pod_network_transmit()),
                    self._query_range(chunk, promql.pod_disk_read()),
                    self._query_range(chunk, promql.pod_disk_write()),
                    self._query_range(chunk, promql.pod_restarts()),
                    self._query_range(chunk, promql.container_memory_working_set()),
                    self._query_range(chunk, promql.pod_ephemeral_storage_usage()),
                    self._query_range(chunk, promql.pod_node()),
                )
            except Exception as error:
                raise RangeCollectionError(
                    f"Prometheus resource queries failed for {chunk_start.isoformat()} -> {chunk_end.isoformat()}"
                ) from error

            net_rx_by_sample = self._pod_series_by_sample(net_rx_results, chunk)
            net_tx_by_sample = self._pod_series_by_sample(net_tx_results, chunk)
            disk_read_by_sample = self._pod_series_by_sample(disk_read_results, chunk)
            disk_write_by_sample = self._pod_series_by_sample(disk_write_results, chunk)
            restart_by_sample = self._pod_series_by_sample(restart_results, chunk)
            ephemeral_usage_by_sample = self._pod_series_by_sample(ephemeral_usage_results, chunk)
            memory_by_sample, container_memory_samples = self._container_series_by_sample(memory_results, chunk)

            # Pod -> node placement from kube-state-metrics, for cAdvisor series without `node` (A10).
            pod_info_node_by_sample: dict[int, dict[tuple[str, str], str]] = defaultdict(dict)
            pod_info_node_latest: dict[tuple[str, str], str] = {}
            for series in pod_node_results or []:
                series_ns, pod, _container, node = self._series_identity(series)
                if not series_ns or not pod or not node:
                    continue
                for sample_start, _value in self._iter_window_samples(series, chunk):
                    pod_info_node_by_sample[sample_start][(series_ns, pod)] = node
                pod_info_node_latest[(series_ns, pod)] = node

            del net_rx_results, net_tx_results, disk_read_results, disk_write_results, restart_results
            del memory_results, ephemeral_usage_results, pod_node_results

            # Parse CPU results into per-sample pod and container usage (cores).
            samples: dict[int, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
            normalized_container_samples: dict[int, dict[tuple[str, str, str], float]] = defaultdict(
                lambda: defaultdict(float)
            )
            pod_node_map_by_ts: dict[int, dict[tuple[str, str], str]] = defaultdict(dict)
            for series in results:
                series_ns, pod, container, node = self._series_identity(series)
                if not series_ns or not pod:
                    continue
                key = (series_ns, pod)
                for sample_start, usage in self._iter_window_samples(series, chunk):
                    samples[sample_start][key] += usage
                    if container:
                        normalized_container_samples[sample_start][(series_ns, pod, container)] += usage
                    resolved_node = (
                        node or pod_info_node_by_sample.get(sample_start, {}).get(key) or pod_info_node_latest.get(key)
                    )
                    if resolved_node:
                        pod_node_map_by_ts[sample_start][key] = resolved_node

            del results

            cpu_usage_by_sample = {
                sample_start: {key: int(round(cores * 1000)) for key, cores in pod_map.items()}
                for sample_start, pod_map in samples.items()
            }

            # Compute energy metrics for this chunk
            chunk_energy_metrics = []
            latest_sample_ts = max(samples) if samples else None
            current_chunk = current_window and chunk_end == end_dt
            for sample_start, pod_map in sorted(samples.items()):
                sample_dt = datetime.fromtimestamp(sample_start, tz=timezone.utc)

                node_total_cpu = defaultdict(float)
                node_pod_map = defaultdict(list)
                for pod_key, cpu in pod_map.items():
                    node = pod_node_map_by_ts.get(sample_start, {}).get(pod_key) or ""
                    node_total_cpu[node] += cpu
                    node_pod_map[node].append((pod_key, cpu))

                for node_name, pods_on_node in node_pod_map.items():
                    power = profile_for_node(node_name, sample_dt)
                    node_power_sources[(node_name, sample_start)] = power.source
                    if power.is_fallback:
                        unknown_power_nodes.add(node_name)
                    calculated_metrics = estimator.calculate_node_energy(
                        node_name=node_name,
                        node_profile=power.profile,
                        node_total_cpu=node_total_cpu.get(node_name, 0.0),
                        pods_on_node=pods_on_node,
                        duration_seconds=chosen_step_sec,
                        estimation_reasons=list(power.reasons),
                    )
                    for m in calculated_metrics:
                        m.timestamp = sample_dt
                        chunk_energy_metrics.append(m)

            del samples, pod_node_map_by_ts

            # Prefetch carbon intensities
            for em in chunk_energy_metrics:
                node_name: str = em.node or ""
                context = node_contexts.get(node_name) if node_name else None
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
                node_name: str = em.node or ""
                joules = em.joules
                ts = em.timestamp
                # Per-sample resource usage: each row carries its own step, never a chunk aggregate.
                sample_key = int(ts.timestamp())
                range_cpu_usage_map = cpu_usage_by_sample.get(sample_key, {})
                range_memory_map = memory_by_sample.get(sample_key, {})
                range_net_rx_map = net_rx_by_sample.get(sample_key, {})
                range_net_tx_map = net_tx_by_sample.get(sample_key, {})
                range_disk_read_map = disk_read_by_sample.get(sample_key, {})
                range_disk_write_map = disk_write_by_sample.get(sample_key, {})
                range_restart_map = restart_by_sample.get(sample_key, {})
                range_ephemeral_usage_map = ephemeral_usage_by_sample.get(sample_key, {})
                node_context = node_contexts.get(node_name) if node_name else None
                zone = node_context.emaps_zone if node_context else self._config.DEFAULT_ZONE
                _ni_prov = nodes_info.get(node_name)
                provider = _ni_prov.cloud_provider if _ni_prov else None
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
                    _ni = nodes_info.get(node_name)
                    aggregate_metric = CombinedMetric(
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
                        node=node_name or None,
                        node_instance_type=(_ni.instance_type if _ni else current_node_map.get(node_name)),
                        node_zone=(_ni.zone if _ni else None),
                        emaps_zone=zone,
                        is_estimated=is_estimated,
                        estimation_reasons=estimation_reasons,
                        embodied_co2e_grams=embodied_emissions_grams,
                        cpu_usage_millicores=range_cpu_usage_map.get(pod_key),
                        memory_usage_bytes=(int(range_memory_map[pod_key]) if pod_key in range_memory_map else None),
                        network_receive_bytes=range_net_rx_map.get(pod_key),
                        network_transmit_bytes=range_net_tx_map.get(pod_key),
                        disk_read_bytes=range_disk_read_map.get(pod_key),
                        disk_write_bytes=range_disk_write_map.get(pod_key),
                        storage_request_bytes=(
                            int(pod_storage_map[pod_key]["request"]) or None if pod_key in pod_storage_map else None
                        ),
                        storage_usage_bytes=(
                            int(pod_storage_map[pod_key]["usage"]) or None if pod_key in pod_storage_map else None
                        ),
                        ephemeral_storage_request_bytes=(pod_ephemeral_storage_map.get(pod_key) or None),
                        ephemeral_storage_usage_bytes=(
                            int(range_ephemeral_usage_map[pod_key]) if pod_key in range_ephemeral_usage_map else None
                        ),
                        restart_count=(int(range_restart_map[pod_key]) if pod_key in range_restart_map else None),
                        calculation_version=CALCULATION_VERSION,
                    )
                    combined.append(aggregate_metric)

                    container_cpu_usage = {
                        key: int(round(value * 1000))
                        for key, value in normalized_container_samples.get(sample_key, {}).items()
                    }
                    container_memory_usage = {
                        key: int(round(value)) for key, value in container_memory_samples.get(sample_key, {}).items()
                    }
                    container_sample_count_map = {}
                    # Point-in-time metrics-server usage only fills gaps of the latest
                    # sample of a current window, and only when it was measured during
                    # or after that sample (A5, BUG-05).  The row keeps its own
                    # timestamp and step duration.
                    if current_chunk and sample_key == latest_sample_ts:
                        freshness_cutoff = max(now - timedelta(seconds=max(chosen_step_sec * 2, 60)), ts)
                        for usage in container_usages:
                            if (
                                usage.timestamp is None
                                or usage.timestamp < freshness_cutoff
                                or usage.timestamp > now + timedelta(seconds=chosen_step_sec)
                            ):
                                continue
                            container_key = (usage.namespace, usage.pod_name, usage.container_name)
                            usage_added = False
                            if usage.cpu_usage_millicores is not None and container_key not in container_cpu_usage:
                                container_cpu_usage[container_key] = usage.cpu_usage_millicores
                                usage_added = True
                            if usage.memory_usage_bytes is not None and container_key not in container_memory_usage:
                                container_memory_usage[container_key] = usage.memory_usage_bytes
                                usage_added = True
                            if usage_added:
                                container_sample_count_map[container_key] = 1
                    if container_cpu_usage or container_memory_usage:
                        container_maps = PodResourceMaps(
                            container_cpu_usage_map=container_cpu_usage,
                            container_memory_usage_map=container_memory_usage,
                            container_sample_count_map=container_sample_count_map,
                        )
                        combined.extend(
                            self.assembler.assemble_container_metrics(
                                [aggregate_metric],
                                pod_metrics_list,
                                container_maps,
                            )
                        )

            del chunk_energy_metrics, cpu_usage_by_sample, memory_by_sample
            del net_rx_by_sample, net_tx_by_sample, disk_read_by_sample, disk_write_by_sample
            del restart_by_sample, ephemeral_usage_by_sample
            del container_memory_samples, normalized_container_samples

        collection_telemetry.set_unknown_power_profile_nodes(len(unknown_power_nodes))
        if unknown_power_nodes:
            logger.warning(
                "No instance type or CPU capacity for node(s) %s; energy uses DEFAULT_INSTANCE_PROFILE.",
                ", ".join(sorted(name or "<unresolved>" for name in unknown_power_nodes)),
            )

        if namespace:
            combined = [c for c in combined if c.namespace == namespace]

        await self.calculator.clear_cache()
        return combined
