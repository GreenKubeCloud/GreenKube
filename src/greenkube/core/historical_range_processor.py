# src/greenkube/core/historical_range_processor.py
"""Processes historical time ranges by querying Prometheus range API."""

import asyncio
import inspect
import logging
import math
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import List

from .. import __version__
from ..collectors import dcgm_collector
from ..collectors.metrics_server_collector import MetricsServerCollector
from ..collectors.node_collector import NodeCollector
from ..collectors.opencost_collector import OpenCostCollector, opencost_step_share
from ..collectors.pod_collector import PodCollector
from ..collectors.prometheus_collector import PrometheusCollector
from ..collectors.promql import PromQLCatalog
from ..core import collection_telemetry
from ..core.calculator import CarbonCalculator
from ..core.collection import CollectionWindow
from ..core.config import Config, get_config
from ..core.metric_assembler import (
    MetricAssembler,
    cost_fields,
    provenance_estimation_reasons,
)
from ..core.node_zone_mapper import NodeZoneMapper
from ..core.prometheus_resource_mapper import PodResourceMaps
from ..energy.estimator import BasicEstimator
from ..energy.node_power import NodePowerProfile, resolve_node_power_profile
from ..impact.power import GpuDevice, GpuPowerModel, NodeGpuPower, node_gpu_power
from ..impact.realization import refresh_realization_context
from ..impact.rows import MetricRowAllocator, NodeIdleMetric, NodeSpec, embodied_g_per_hour
from ..models.metrics import CombinedMetric, EnergyMetric, PodMetric
from ..models.node import NodeInfo, NodeZoneContext
from ..models.provenance import MetadataSource, NodeSource, UsageSource
from ..storage.base_pod_snapshot_repository import PodSnapshotRepository, snapshot_at
from ..storage.base_repository import CarbonIntensityRepository, CombinedMetricsRepository, NodeRepository
from ..utils.time import ensure_utc, parse_iso_date, parse_query_step_seconds

logger = logging.getLogger(__name__)

#: Bumped when the range pipeline changes how stored values are computed
#: (A1: pod cgroup no longer double counted; A2: half-open per-step samples;
#: A10: capacity-derived power profiles; B2: energy, CO2e and modeled cost
#: allocated by the Impact Model, see ``model_version``; range.4, WP-01.1: node
#: attributes and pod metadata as of the sample time, from node and pod
#: snapshots).  Rows written before carry the bare package version and
#: over-state CPU, memory and energy; ``+range.3`` rows keep the node and pod
#: state of their collection time (``node_source``/``metadata_source`` NULL).
CALCULATION_VERSION = f"{__version__}+range.4"
# Day-sized chunks bound the memory of long backfills.
_CHUNK_SECONDS = 86400
# Prometheus evaluation timestamps are exact multiples of the step; tolerate float noise.
_TIMESTAMP_TOLERANCE = 1e-3
#: Smallest real cgroup memory figure: one page.
_MEMORY_PAGE_BYTES = 4096
#: Metrics API container usage may exceed its pod cgroup by this share (sampling skew) before it is refitted.
POD_CGROUP_TOLERANCE = 0.05
#: Estimation reason of container usage refitted to its pod cgroup (USER BUG 2).
CONTAINER_USAGE_EXCEEDS_POD = "container_usage_exceeds_pod"
#: Estimation reason of a pod row without any pod metadata (no history, not in the live pod list).
POD_METADATA_MISSING = "pod_metadata_missing"
#: Pod history versions read around the range: a pod of the range was observed at most this far outside it.
_POD_SNAPSHOT_MARGIN = timedelta(hours=1)


class RangeCollectionError(RuntimeError):
    """A source (Prometheus, the Kubernetes API) could not be read for a window; the window must be retried."""


async def _list_strictly(collect):
    """Call a pod or node collector so that an API failure raises instead of returning nothing (COL-07).

    Collectors that do not take ``raise_on_error`` (test doubles, third-party
    collectors) are called as before.
    """
    try:
        parameters = inspect.signature(collect).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "raise_on_error" in parameters:
        return await collect(raise_on_error=True)
    return await collect()


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
        pod_snapshot_repository: PodSnapshotRepository | None = None,
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
        #: Pod metadata history (WP-01.1); without it pod metadata comes from the live pod list only.
        self.pod_snapshot_repository = pod_snapshot_repository
        self._promql = PromQLCatalog.from_config(self._config)
        self._allocator = MetricRowAllocator(self._config)
        self._gpu_model = GpuPowerModel()
        #: Idle rows (``__idle__`` per node and step) of the last ``run_range`` call.
        self.last_idle_metrics: List[NodeIdleMetric] = []

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
    def _deduplicated_samples(
        cls, results, chunk: CollectionWindow
    ) -> tuple[dict[int, dict[tuple[str, str, str], float]], dict[int, dict[tuple[str, str], str]]]:
        """One value per ``(namespace, pod, container)`` and sample; ``container`` is ``""`` for a pod cgroup.

        Several series of one container (``/metrics/resource`` next to
        ``/metrics/cadvisor``, two scrape jobs, two cgroup paths, or variants of
        the ``node`` label) are duplicates, not parts: they are merged with
        ``max``, never summed (USER BUG 2). Also returns the node of each pod.
        """
        values: dict[int, dict[tuple[str, str, str], float]] = defaultdict(dict)
        nodes: dict[int, dict[tuple[str, str], str]] = defaultdict(dict)
        for series in results or []:
            namespace, pod, container, node = cls._series_identity(series)
            if not namespace or not pod:
                continue
            key = (namespace, pod, container)
            for sample_start, value in cls._iter_window_samples(series, chunk):
                current = values[sample_start].get(key)
                values[sample_start][key] = value if current is None else max(current, value)
                if node:
                    nodes[sample_start].setdefault((namespace, pod), node)
        return values, nodes

    @classmethod
    def _container_series_by_sample(
        cls, results, chunk: CollectionWindow
    ) -> tuple[dict[int, dict[tuple[str, str], float]], dict[int, dict[tuple[str, str, str], float]]]:
        """Return pod sums and per-container values for every sample of the chunk.

        A pod's value is the sum of its (deduplicated) containers, or its pod
        cgroup value when only pod-level series exist. A pod cgroup working set
        below one memory page is a gap in cAdvisor's stats (a running pod
        always holds pages; Minikube reported 0 or 8 bytes for hours), not a
        measurement: it is left out.
        """
        values, _nodes = cls._deduplicated_samples(results, chunk)
        pods: dict[int, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
        containers: dict[int, dict[tuple[str, str, str], float]] = defaultdict(lambda: defaultdict(float))
        for sample_start, by_key in values.items():
            named_pods = {(namespace, pod) for namespace, pod, container in by_key if container}
            for (namespace, pod, container), value in by_key.items():
                if container:
                    containers[sample_start][(namespace, pod, container)] = value
                    pods[sample_start][(namespace, pod)] += value
                elif (namespace, pod) not in named_pods and value >= _MEMORY_PAGE_BYTES:
                    pods[sample_start][(namespace, pod)] = value
        return pods, containers

    @staticmethod
    def _fit_to_pod_cgroup(
        container_usage: dict[tuple[str, str, str], int],
        filled: set[tuple[str, str, str]],
        pod_usage: dict[tuple[str, str], float] | dict[tuple[str, str], int],
        measured_pods: set[tuple[str, str]],
    ) -> set[tuple[str, str, str]]:
        """Scale Metrics API container usage down to its pod's cgroup when the sum exceeds it.

        The containers of a pod live inside the pod cgroup, so their usage can
        never add up to more than the pod's. Some kubelets (cri-dockerd, e.g.
        Minikube's Docker driver) report a container working set that includes
        page cache the pod cgroup's working set excludes: ~665Mi for a
        container whose pod measures ~560Mi (USER BUG 2). When the sum of the
        filled containers exceeds the pod cgroup value from Prometheus for the
        same step by more than :data:`POD_CGROUP_TOLERANCE`, every container is
        scaled by ``pod / sum``, keeping their split. Only pods whose
        Prometheus value is the pod cgroup itself (no container series in
        ``measured_pods``) are checked. Mutates ``container_usage`` and returns
        the refitted keys, which the caller marks as estimated.
        """
        by_pod: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
        for key in filled:
            by_pod[(key[0], key[1])].append(key)
        refitted: set[tuple[str, str, str]] = set()
        for pod_key, keys in by_pod.items():
            if pod_key in measured_pods:
                continue
            pod_value = pod_usage.get(pod_key)
            if pod_value is None or pod_value <= 0:
                continue
            total = sum(max(container_usage[key], 0) for key in keys)
            if total <= pod_value * (1 + POD_CGROUP_TOLERANCE):
                continue
            factor = pod_value / total
            for key in keys:
                container_usage[key] = int(max(container_usage[key], 0) * factor)
                refitted.add(key)
        return refitted

    async def _query_range(self, chunk: CollectionWindow, query: str):
        return await self.prometheus_collector.collect_range(
            start=chunk.first_evaluation,
            end=chunk.last_evaluation,
            step=self._promql.step,
            query=query,
        )

    async def _collect_cpu(self, chunk: CollectionWindow):
        """Container CPU series, or the pod cgroup series (counted once) when no container series exist.

        A query error fails the window instead of degrading to the fallback, so a
        transient outage never stores a window without its container breakdown.
        """
        try:
            results = await self._query_range(chunk, self._promql.container_cpu_usage())
            if results:
                return results
            return await self._query_range(chunk, self._promql.pod_cgroup_cpu_usage())
        except Exception as error:
            raise RangeCollectionError(
                f"Prometheus CPU usage is unavailable for {chunk.start.isoformat()} -> {chunk.end.isoformat()}"
            ) from error

    async def _collect_memory(self, chunk: CollectionWindow):
        """Container working-set series, or the pod cgroup series when no container series exist.

        The pod cgroup fallback gives the pod-level working set only: it never
        becomes container usage (rightsizing reports ``pod_level_usage_only``).
        """
        results = await self._query_range(chunk, self._promql.container_memory_working_set())
        if results:
            return results
        return await self._query_range(chunk, self._promql.pod_cgroup_memory_working_set())

    async def _collect_gpu(self, chunk: CollectionWindow) -> dict[int, list[dcgm_collector.GpuSample]]:
        """DCGM GPU power and utilization per step; empty when DCGM is absent or unreachable (B5)."""
        step = self._promql.step
        try:
            power, utilization = await asyncio.gather(
                self._query_range(chunk, dcgm_collector.power_query(step)),
                self._query_range(chunk, dcgm_collector.utilization_query(step)),
            )
        except Exception as error:
            logger.warning(
                "DCGM GPU metrics unavailable for %s -> %s: %s; GPU power is not counted for this window.",
                chunk.start.isoformat(),
                chunk.end.isoformat(),
                error,
            )
            return {}
        return dcgm_collector.samples_by_step(
            power, utilization, lambda series: self._iter_window_samples(series, chunk)
        )

    def _gpu_power_by_group(
        self,
        gpu_by_sample: dict[int, list[dcgm_collector.GpuSample]],
        groups: dict[tuple[int, str], list[tuple[str, str]]],
        pod_node_map_by_ts: dict[int, dict[tuple[str, str], str]],
        pod_node_latest: dict[tuple[str, str], str],
        known_nodes: set[str],
    ) -> dict[tuple[int, str], NodeGpuPower]:
        """GPU power of every (sample, node) group, attributed to the group's pods.

        A GPU is placed on its pod's node, else on the first label naming a
        known node, else on the node of the exporter pod. Power attributed to a
        pod without a row in the group is counted as unattributed (``__idle__``),
        so no measured GPU energy is dropped.
        """
        if not gpu_by_sample:
            return {}
        exporter_nodes = {pod: node for (_ns, pod), node in pod_node_latest.items()}
        result: dict[tuple[int, str], NodeGpuPower] = {}
        for sample_start, gpu_samples in gpu_by_sample.items():
            placement = pod_node_map_by_ts.get(sample_start, {})
            devices: dict[str, list[GpuDevice]] = defaultdict(list)
            for gpu in gpu_samples:
                identity = gpu.identity
                pod_key = (identity.namespace, identity.pod) if identity.namespace and identity.pod else None
                node = (placement.get(pod_key) or pod_node_latest.get(pod_key)) if pod_key else None
                if not node:
                    node = next((hint for hint in identity.node_hints if hint in known_nodes), None)
                if not node:
                    for hint in (identity.exporter_pod, *identity.node_hints):
                        if hint and hint in exporter_nodes:
                            node = exporter_nodes[hint]
                            break
                if not node:
                    continue
                devices[node].append(
                    GpuDevice(
                        node=node,
                        device=identity.device,
                        model_name=identity.model_name,
                        power_watts=gpu.power_watts,
                        utilization_pct=gpu.utilization_pct,
                        namespace=identity.namespace,
                        pod=identity.pod,
                    )
                )
            for node, node_devices in devices.items():
                power = node_gpu_power(node_devices, self._gpu_model)
                pods = set(groups.get((sample_start, node), ()))
                orphaned = sum(watts for key, watts in power.by_pod.items() if key not in pods)
                if orphaned:
                    power = NodeGpuPower(
                        by_pod={key: watts for key, watts in power.by_pod.items() if key in pods},
                        usage_by_pod={key: usage for key, usage in power.usage_by_pod.items() if key in pods},
                        unattributed_watts=power.unattributed_watts + orphaned,
                        sources=power.sources,
                    )
                result[(sample_start, node)] = power
        return result

    @staticmethod
    def _node_spec(
        node_name: str,
        info: NodeInfo | None,
        power: NodePowerProfile,
        boavizta_cache: dict,
        *,
        instance_type: str | None,
        gpu_unattributed_watts: float,
    ) -> NodeSpec:
        """What the Impact Model needs to know about one node at one step."""
        profile = power.profile or {}
        embodied_profile = None
        if info is not None and info.cloud_provider and info.instance_type:
            embodied_profile = boavizta_cache.get((info.cloud_provider, info.instance_type))
        return NodeSpec(
            name=node_name,
            instance_type=instance_type,
            provider=info.cloud_provider if info else None,
            region=info.region if info else None,
            cpu_capacity_cores=info.cpu_capacity_cores if info else None,
            memory_capacity_bytes=info.memory_capacity_bytes if info else None,
            idle_watts=float(profile.get("minWatts", 0.0) or 0.0),
            max_watts=float(profile.get("maxWatts", 0.0) or 0.0),
            vcores=float(profile.get("vcores", 0.0) or 0.0) or None,
            power_source=power.source,
            embodied_g_per_hour=embodied_g_per_hour(embodied_profile),
            gpu_unattributed_watts=gpu_unattributed_watts,
            is_fallback=power.is_fallback,
        )

    async def _pod_history(
        self,
        live_pods: List[PodMetric],
        now: datetime,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> dict:
        """Record the live pod list and read the pod history around ``[start, end)`` (WP-01.1).

        Returns ``{(namespace, pod): [PodSnapshot, ...]}``.  Without a history
        repository, or when it fails, the result is empty and pod metadata
        comes from the live pod list; rows of other pods get
        ``metadata_source = missing``, so the degradation is visible per row.
        """
        repository = self.pod_snapshot_repository
        if repository is None:
            return {}
        margin = max(_POD_SNAPSHOT_MARGIN, timedelta(seconds=step_seconds))
        try:
            if live_pods:
                await repository.save_pods(live_pods, now)
            # No upper bound: a version first observed after the range is the
            # nearest one for a pod first seen after its samples.
            return await repository.get_snapshots(start - margin, now + margin)
        except Exception as error:
            logger.warning(
                "Pod metadata history unavailable (%s); pods missing from the live pod list get "
                "metadata_source=missing for %s -> %s.",
                error,
                start.isoformat(),
                end.isoformat(),
            )
            return {}

    async def _require_recent_pod_history(
        self, error: Exception, now: datetime, step_seconds: int, start: datetime, end: datetime
    ) -> None:
        """Fail the window after a failed pod list unless the pod history is fresh (COL-07).

        With a pod snapshot observed within one step the window proceeds from
        the history (rows say ``metadata_source = k8s_snapshot``); otherwise it
        raises :class:`RangeCollectionError` so the watermark does not move.
        """
        window = f"{start.isoformat()} -> {end.isoformat()}"
        repository = self.pod_snapshot_repository
        recent = {}
        if repository is not None:
            try:
                recent = await repository.get_snapshots(
                    now - timedelta(seconds=step_seconds), now + _POD_SNAPSHOT_MARGIN
                )
            except Exception as history_error:
                logger.warning("Pod metadata history unavailable: %s", history_error)
        if not recent:
            raise RangeCollectionError(
                f"Kubernetes pod list failed for {window} and no pod snapshot is newer than one step: {error}"
            ) from error
        logger.warning(
            "Kubernetes pod list failed (%s); pod metadata comes from the pod history for %s.", error, window
        )

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
        self.last_idle_metrics = []

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

        # Pod metadata (owner, requests, containers) as of each sample (WP-01.1, COL-01).
        try:
            pod_metrics_list = await _list_strictly(pod_collector.collect)
            pod_list_error: Exception | None = None
        except Exception as error:  # KubernetesListError, or any failure of another collector
            pod_metrics_list = []
            pod_list_error = error
            collection_telemetry.record_k8s_list_error("pods")
        if pod_list_error is not None:
            await self._require_recent_pod_history(pod_list_error, now, chosen_step_sec, start_dt, end_dt)
        live_pods: dict[tuple[str, str], list[PodMetric]] = defaultdict(list)
        for pod_metric in pod_metrics_list:
            live_pods[(pod_metric.namespace, pod_metric.pod_name)].append(pod_metric)
        pod_history = await self._pod_history(pod_metrics_list, now, start_dt, end_dt, chosen_step_sec)

        def pod_metadata_at(pod_key: tuple[str, str], timestamp: datetime) -> tuple[list[PodMetric], MetadataSource]:
            """Containers (with owner and requests) of a pod as of ``timestamp``.

            The history version valid at ``timestamp`` (or the nearest one),
            else the live pod list, else nothing (``missing``): a pod deleted
            before its window was collected keeps the metadata it had.
            """
            versions = pod_history.get(pod_key)
            if versions:
                version = snapshot_at(versions, timestamp)
                if version is not None:
                    return version.pod_metrics(), MetadataSource.K8S_SNAPSHOT
            live = live_pods.get(pod_key)
            if live:
                return live, MetadataSource.K8S_SNAPSHOT
            return [], MetadataSource.MISSING

        # Current node metadata — collected once here for zone mapping and
        # assembly.  Historical snapshots (node_timeline) cover the past;
        # this provides a best-effort fallback for nodes not yet in the DB.
        try:
            nodes_info = await _list_strictly(self.node_collector.collect) or {}
        except Exception as error:  # KubernetesListError, or any failure of another collector
            collection_telemetry.record_k8s_list_error("nodes")
            if not node_timeline:
                raise RangeCollectionError(
                    f"Kubernetes node list failed for {start_dt.isoformat()} -> {end_dt.isoformat()} and no node "
                    f"snapshot exists: {error}"
                ) from error
            logger.warning(
                "Kubernetes node list failed (%s); node attributes come from node snapshots for %s -> %s.",
                error,
                start_dt.isoformat(),
                end_dt.isoformat(),
            )
            nodes_info = {}

        def node_at(node_name: str, timestamp: datetime) -> tuple[NodeInfo | None, NodeSource]:
            """Node attributes as of ``timestamp`` (WP-01.1, RECON-05).

            The snapshot valid at ``timestamp``, else the earliest snapshot after
            it (a node first saved after the sample), else the node as the API
            lists it now.  A node that left the cluster before the window was
            collected therefore keeps its real zone, provider and instance type.
            """
            if not node_name:
                return None, NodeSource.UNKNOWN
            info = get_node_info_at(node_name, timestamp)
            if info is None:
                info = next((later for ts, later in node_timeline.get(node_name, ()) if ts > timestamp), None)
            if info is not None:
                return info, NodeSource.SNAPSHOT
            current = nodes_info.get(node_name)
            if current is not None:
                return current, NodeSource.CURRENT_API
            return None, NodeSource.UNKNOWN

        def profile_for_node(node_name: str, info: NodeInfo | None) -> NodePowerProfile:
            """Resolve the node power profile with provenance (A10) from the node as of the sample."""
            current_info = nodes_info.get(node_name) if node_name else None
            instance_type = (info.instance_type if info else None) or current_node_map.get(node_name)
            cpu_capacity = None
            for candidate in (info, current_info):
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

        # Zone contexts are mapped from the node as of each sample (snapshot or
        # current), never by a Kubernetes API call; one mapping per node version.
        zone_context_cache: dict[tuple, NodeZoneContext | None] = {}

        async def zone_context_for(node_name: str, info: NodeInfo | None) -> NodeZoneContext | None:
            if not node_name or info is None:
                return None
            cache_key = (node_name, info.zone, info.region, info.cloud_provider)
            if cache_key not in zone_context_cache:
                contexts = await self.zone_mapper.map_nodes({node_name: info})
                zone_context_cache[cache_key] = (contexts or {}).get(node_name)
            return zone_context_cache[cache_key]

        # Boavizta embodied profiles for every node version the window can use:
        # the current nodes and every snapshot of the timeline (RECON-05).
        embodied_nodes: dict[str, NodeInfo] = dict(nodes_info)
        for node_name, timeline in node_timeline.items():
            for index, (_ts, snapshot_info) in enumerate(timeline):
                embodied_nodes[f"{node_name}#snapshot{index}"] = snapshot_info
        try:
            boavizta_cache = await self.assembler.embodied_service.prepare_embodied_data(embodied_nodes)
        except Exception as e:
            logger.warning("Failed to prepare embodied data: %s. Embodied emissions will be 0.", e)
            boavizta_cache = {}

        # Cost data
        range_seconds = (end_dt - start_dt).total_seconds()
        try:
            # Exact-window allocation keyed by (namespace, pod) (A4, BUG-04, BUG-41).
            cost_metrics = await self.opencost_collector.collect_range(start=start_dt, end=end_dt)
            cost_map = {(c.namespace, c.pod_name): c for c in cost_metrics}
        except Exception:
            cost_map = {}

        # Per-pod persistent storage (PVC) request/usage — best-effort.
        try:
            pod_storage_map = await self.prometheus_collector.collect_pod_storage()
        except Exception as e:
            logger.warning("Failed to collect pod storage metrics: %s", e)
            pod_storage_map = {}

        # Realization inputs (autoscalers, consolidation policies, disruption
        # blockers) are refreshed from the controller's collection cycle (B6).
        try:
            await refresh_realization_context(self._config)
        except Exception as error:  # never fail a collection window for it
            logger.warning("Could not refresh the realization context: %s", error)

        # --- Chunked processing ---
        combined: List[CombinedMetric] = []
        idle_rows: List[NodeIdleMetric] = []
        allocator = self._allocator
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
                    self._collect_memory(chunk),
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
            # Duplicate series of one container are merged with max (USER BUG 2).
            cpu_values, cpu_nodes = self._deduplicated_samples(results, chunk)
            for sample_start, by_key in cpu_values.items():
                named_pods = {(series_ns, pod) for series_ns, pod, container in by_key if container}
                for (series_ns, pod, container), usage in by_key.items():
                    key = (series_ns, pod)
                    if container:
                        samples[sample_start][key] += usage
                        normalized_container_samples[sample_start][(series_ns, pod, container)] = usage
                    elif key not in named_pods:
                        samples[sample_start][key] = usage
                    resolved_node = (
                        cpu_nodes.get(sample_start, {}).get(key)
                        or pod_info_node_by_sample.get(sample_start, {}).get(key)
                        or pod_info_node_latest.get(key)
                    )
                    if resolved_node:
                        pod_node_map_by_ts[sample_start][key] = resolved_node
            del cpu_values, cpu_nodes

            del results

            cpu_usage_by_sample = {
                sample_start: {key: int(round(cores * 1000)) for key, cores in pod_map.items()}
                for sample_start, pod_map in samples.items()
            }

            # Pods of every (sample, node): the allocation unit of the Impact Model (B2).
            groups: dict[tuple[int, str], list[tuple[str, str]]] = defaultdict(list)
            for sample_start, pod_map in sorted(samples.items()):
                for pod_key in pod_map:
                    node = pod_node_map_by_ts.get(sample_start, {}).get(pod_key) or ""
                    groups[(sample_start, node)].append(pod_key)
            latest_sample_ts = max(samples) if samples else None
            current_chunk = current_window and chunk_end == end_dt
            gpu_power_by_group = self._gpu_power_by_group(
                await self._collect_gpu(chunk),
                groups,
                pod_node_map_by_ts,
                pod_info_node_latest,
                set(nodes_info) | {node for _start, node in groups if node},
            )

            del samples, pod_node_map_by_ts

            # Node attributes and zone context of every (sample, node), as of the sample.
            node_state: dict[tuple[int, str], tuple[NodeInfo | None, NodeSource, NodeZoneContext | None]] = {}
            for sample_start, node_name in groups:
                info, node_source = node_at(node_name, datetime.fromtimestamp(sample_start, tz=timezone.utc))
                node_state[(sample_start, node_name)] = (
                    info,
                    node_source,
                    await zone_context_for(node_name, info),
                )

            # Prefetch carbon intensities, once per zone and sample.
            for sample_start, node_name in groups:
                context = node_state[(sample_start, node_name)][2]
                zone = context.emaps_zone if context else self._config.DEFAULT_ZONE
                ts = datetime.fromtimestamp(sample_start, tz=timezone.utc)
                try:
                    intensity = await repository.get_for_zone_at_time(zone, ts.isoformat())
                except Exception:
                    intensity = None
                if intensity is not None:
                    await calculator.prefetch_intensity(zone, ts.isoformat(), intensity)

            # Build and allocate the pod rows of every (sample, node).
            for (sample_start, node_name), pod_keys in sorted(groups.items()):
                ts = datetime.fromtimestamp(sample_start, tz=timezone.utc)
                sample_key = sample_start
                _ni, node_source, node_context = node_state[(sample_start, node_name)]
                power = profile_for_node(node_name, _ni)
                node_power_sources[(node_name, sample_start)] = power.source
                if power.is_fallback:
                    unknown_power_nodes.add(node_name)
                range_cpu_usage_map = cpu_usage_by_sample.get(sample_key, {})
                range_memory_map = memory_by_sample.get(sample_key, {})
                range_net_rx_map = net_rx_by_sample.get(sample_key, {})
                range_net_tx_map = net_tx_by_sample.get(sample_key, {})
                range_disk_read_map = disk_read_by_sample.get(sample_key, {})
                range_disk_write_map = disk_write_by_sample.get(sample_key, {})
                range_restart_map = restart_by_sample.get(sample_key, {})
                range_ephemeral_usage_map = ephemeral_usage_by_sample.get(sample_key, {})
                zone = node_context.emaps_zone if node_context else self._config.DEFAULT_ZONE
                provider = _ni.cloud_provider if _ni else None
                pue = self._config.get_pue_for_provider(provider)
                try:
                    # Energy comes from the allocation below; the calculator
                    # resolves the intensity and its provenance (A8).
                    carbon_result = await calculator.calculate_emissions(joules=0.0, zone=zone, timestamp=ts, pue=pue)
                except Exception as error:
                    # Never drop the usage, cost and energy rows of the group for
                    # a missing CO2e factor: fail the window so it is retried (COL-08).
                    logger.error(
                        "Carbon intensity lookup failed for zone %s at %s (%d pod row(s) of node %s); "
                        "the window will be retried: %s",
                        zone,
                        ts.isoformat(),
                        len(pod_keys),
                        node_name or "<unresolved>",
                        error,
                    )
                    raise RangeCollectionError(
                        f"Carbon intensity lookup failed for zone {zone} at {ts.isoformat()}: {error}"
                    ) from error
                if carbon_result is None:
                    raise RangeCollectionError(f"No carbon intensity result for zone {zone} at {ts.isoformat()}")
                intensity_source = getattr(carbon_result, "intensity_source", None)
                if not isinstance(intensity_source, str):
                    intensity_source = None
                fallback_reasons = provenance_estimation_reasons(intensity_source, power.source)
                gpu_power = gpu_power_by_group.get((sample_start, node_name))

                node_rows: List[CombinedMetric] = []
                group_pod_metrics: List[PodMetric] = []
                for em_namespace, pod_name in pod_keys:
                    pod_key = (em_namespace, pod_name)
                    pod_containers, metadata_source = pod_metadata_at(pod_key, ts)
                    group_pod_metrics.extend(pod_containers)
                    owner = next(
                        ((c.owner_kind, c.owner_name) for c in pod_containers if c.owner_kind and c.owner_name),
                        None,
                    )
                    energy_metric = EnergyMetric(
                        pod_name=pod_name,
                        namespace=em_namespace,
                        joules=0.0,
                        node=node_name or None,
                        timestamp=ts,
                        is_estimated=bool(power.reasons),
                        estimation_reasons=list(power.reasons),
                        power_source=power.source,
                    )
                    cost_metric = cost_map.get(pod_key)
                    costs = cost_fields(
                        cost_metric,
                        opencost_step_share(cost_metric, chosen_step_sec, range_seconds) if cost_metric else 1.0,
                        self._config.DEFAULT_COST,
                    )
                    is_estimated, estimation_reasons = self.assembler.build_estimation_flags(
                        energy_metric=energy_metric,
                        node_context=node_context,
                        cost_metric=cost_metric,
                        provider=provider,
                        pue=pue,
                        node_name=node_name,
                        cpu_adjusted_nodes=set(),
                    )
                    if fallback_reasons:
                        is_estimated = True
                        estimation_reasons = list(dict.fromkeys([*estimation_reasons, *fallback_reasons]))
                    if metadata_source is MetadataSource.MISSING:
                        is_estimated = True
                        estimation_reasons = list(dict.fromkeys([*estimation_reasons, POD_METADATA_MISSING]))
                    node_rows.append(
                        CombinedMetric(
                            pod_name=pod_name,
                            namespace=em_namespace,
                            owner_kind=owner[0] if owner else None,
                            owner_name=owner[1] if owner else None,
                            period=None,
                            **costs,
                            usage_source=UsageSource.PROMETHEUS.value,
                            power_source=power.source,
                            intensity_source=intensity_source,
                            timestamp=ts,
                            duration_seconds=chosen_step_sec,
                            grid_intensity_timestamp=carbon_result.grid_intensity_timestamp,
                            co2e_grams=0.0,
                            pue=pue,
                            grid_intensity=carbon_result.grid_intensity,
                            joules=0.0,
                            cpu_request=sum(c.cpu_request for c in pod_containers),
                            memory_request=sum(c.memory_request for c in pod_containers),
                            node=node_name or None,
                            node_instance_type=(
                                (_ni.instance_type if _ni else None) or current_node_map.get(node_name)
                            ),
                            node_zone=(_ni.zone if _ni else None),
                            node_source=node_source.value,
                            metadata_source=metadata_source.value,
                            emaps_zone=zone,
                            is_estimated=is_estimated,
                            estimation_reasons=estimation_reasons,
                            embodied_co2e_grams=0.0,
                            cpu_usage_millicores=range_cpu_usage_map.get(pod_key),
                            memory_usage_bytes=(
                                int(range_memory_map[pod_key]) if pod_key in range_memory_map else None
                            ),
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
                            ephemeral_storage_request_bytes=(
                                sum(c.ephemeral_storage_request for c in pod_containers) or None
                            ),
                            ephemeral_storage_usage_bytes=(
                                int(range_ephemeral_usage_map[pod_key])
                                if pod_key in range_ephemeral_usage_map
                                else None
                            ),
                            gpu_power_watts=gpu_power.by_pod.get(pod_key) if gpu_power else None,
                            gpu_usage_millicores=gpu_power.usage_by_pod.get(pod_key) if gpu_power else None,
                            restart_count=(int(range_restart_map[pod_key]) if pod_key in range_restart_map else None),
                            calculation_version=CALCULATION_VERSION,
                        )
                    )

                spec = self._node_spec(
                    node_name,
                    _ni,
                    power,
                    boavizta_cache,
                    instance_type=(_ni.instance_type if _ni else None) or current_node_map.get(node_name),
                    gpu_unattributed_watts=gpu_power.unattributed_watts if gpu_power else 0.0,
                )
                node_profile = allocator.node_profile(
                    spec, node_rows, pue=pue, intensity=carbon_result.grid_intensity, hours=chosen_step_sec / 3600.0
                )
                allocated = allocator.allocate(
                    node_rows,
                    node_profile,
                    chosen_step_sec / 3600.0,
                    emit_idle=bool(node_name),
                    idle_fields={"timestamp": ts, "emaps_zone": zone, "intensity_source": intensity_source},
                )
                if allocated.idle is not None:
                    idle_rows.append(allocated.idle)

                container_cpu_usage = {
                    key: int(round(value * 1000))
                    for key, value in normalized_container_samples.get(sample_key, {}).items()
                }
                container_memory_usage = {
                    key: int(round(value)) for key, value in container_memory_samples.get(sample_key, {}).items()
                }
                container_sample_count_map = {}
                refitted_containers: set[tuple[str, str, str]] = set()
                # Point-in-time metrics-server usage only fills gaps of the latest
                # sample of a current window, and only when it was measured during
                # or after that sample (A5, BUG-05).  The row keeps its own
                # timestamp and step duration.
                if current_chunk and sample_key == latest_sample_ts:
                    cpu_measured_pods = {(key[0], key[1]) for key in container_cpu_usage}
                    memory_measured_pods = {(key[0], key[1]) for key in container_memory_usage}
                    cpu_filled: set[tuple[str, str, str]] = set()
                    memory_filled: set[tuple[str, str, str]] = set()
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
                            cpu_filled.add(container_key)
                            usage_added = True
                        if usage.memory_usage_bytes is not None and container_key not in container_memory_usage:
                            container_memory_usage[container_key] = usage.memory_usage_bytes
                            memory_filled.add(container_key)
                            usage_added = True
                        if usage_added:
                            container_sample_count_map[container_key] = 1
                    # The pod cgroup bounds its containers (USER BUG 2): refit what contradicts it.
                    refitted_containers = self._fit_to_pod_cgroup(
                        container_cpu_usage, cpu_filled, range_cpu_usage_map, cpu_measured_pods
                    ) | self._fit_to_pod_cgroup(
                        container_memory_usage, memory_filled, range_memory_map, memory_measured_pods
                    )
                container_maps = (
                    PodResourceMaps(
                        container_cpu_usage_map=container_cpu_usage,
                        container_memory_usage_map=container_memory_usage,
                        container_sample_count_map=container_sample_count_map,
                    )
                    if container_cpu_usage or container_memory_usage
                    else None
                )
                for aggregate_metric in allocated.rows:
                    combined.append(aggregate_metric)
                    if container_maps is not None:
                        container_rows = self.assembler.assemble_container_metrics(
                            [aggregate_metric],
                            group_pod_metrics,
                            container_maps,
                        )
                        if refitted_containers:
                            container_rows = [
                                _mark_refitted(row) if _container_key(row) in refitted_containers else row
                                for row in container_rows
                            ]
                        combined.extend(container_rows)

            del cpu_usage_by_sample, memory_by_sample, groups, gpu_power_by_group
            del net_rx_by_sample, net_tx_by_sample, disk_read_by_sample, disk_write_by_sample
            del restart_by_sample, ephemeral_usage_by_sample
            del container_memory_samples, normalized_container_samples

        collection_telemetry.set_unknown_power_profile_nodes(len(unknown_power_nodes))
        if unknown_power_nodes:
            logger.warning(
                "No instance type or CPU capacity for node(s) %s; energy uses DEFAULT_INSTANCE_PROFILE.",
                ", ".join(sorted(name or "<unresolved>" for name in unknown_power_nodes)),
            )

        # Idle rows are returned, not written: the caller commits them with the
        # pod rows after its leader-fence check (roadmap A14), so a replica that
        # lost the Lease during the Prometheus queries writes nothing.
        self.last_idle_metrics = idle_rows
        if namespace:
            combined = [c for c in combined if c.namespace == namespace]

        await self.calculator.clear_cache()
        return combined


def _container_key(row: CombinedMetric) -> tuple[str, str, str]:
    return (row.namespace or "", row.pod_name or "", row.container_name or "")


def _mark_refitted(row: CombinedMetric) -> CombinedMetric:
    """Flags a container row whose usage was scaled down to its pod cgroup (explicit evidence flag)."""
    reasons = list(dict.fromkeys([*row.estimation_reasons, CONTAINER_USAGE_EXCEEDS_POD]))
    return row.model_copy(update={"is_estimated": True, "estimation_reasons": reasons})
