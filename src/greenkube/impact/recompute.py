# src/greenkube/impact/recompute.py
"""Recalculation of stored raw rows with the current Impact Model (roadmap B8).

When :data:`~greenkube.impact.types.IMPACT_MODEL_VERSION` changes, raw rows
still within retention can be re-allocated from what they recorded:
requests, usage, node, PUE, grid intensity, observed costs and attributed GPU
power. The node's power profile and embodied emissions come from the node
snapshots. Re-allocation is per node and step, like collection, so pod rows
and the node's ``__idle__`` row keep adding up to the node totals. Container
rows are re-split from their pod row the way collection splits them.

Hourly aggregates are never touched: they keep the model version of the raw
rows they were compressed from. Nothing is re-read from Prometheus or
OpenCost, and the stored intensity is kept (the intensity recompute, A9,
handles late intensities).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.energy.embodied import load_cached_profiles, resolve_node_embodied
from greenkube.energy.node_power import default_profile_from_config, resolve_node_power_profile
from greenkube.energy.shared_host import SHARED_HOST_REASON_PREFIX, SharedHost, shared_host_groups
from greenkube.impact.power import GPU_TDP_ESTIMATE_REASON_PREFIX, GPU_UNMEASURED_REASON_PREFIX
from greenkube.impact.rows import (
    MetricRowAllocator,
    NodeIdleMetric,
    NodeSpec,
    container_cost_shares,
    container_gpu_fields,
    is_modeled_cost,
)
from greenkube.impact.types import HOURS_PER_YEAR, IMPACT_MODEL_VERSION
from greenkube.models.metrics import CombinedMetric

logger = logging.getLogger(__name__)

#: Rows are read and written one hour at a time to bound memory.
RECOMPUTE_CHUNK = timedelta(hours=1)


@dataclass
class RecomputeReport:
    """What a recompute changed (or would change, in a dry run)."""

    start: datetime
    end: datetime
    dry_run: bool
    pod_rows_read: int = 0
    pod_rows_recomputed: int = 0
    container_rows_recomputed: int = 0
    idle_rows_written: int = 0
    groups_skipped_current: int = 0
    #: Node-steps of the current model whose idle row predates the recorded inputs (0184): left as stored.
    groups_skipped_unrecorded: int = 0
    previous_versions: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        versions = ", ".join(
            f"{version or 'none'}: {count}"
            for version, count in sorted(self.previous_versions.items(), key=lambda item: str(item[0]))
        )
        action = "would recompute" if self.dry_run else "recomputed"
        return (
            f"{action} {self.pod_rows_recomputed} pod row(s), {self.container_rows_recomputed} container row(s) "
            f"and {self.idle_rows_written} idle row(s) to model {IMPACT_MODEL_VERSION} "
            f"between {self.start.isoformat()} and {self.end.isoformat()} "
            f"({self.pod_rows_read} pod row(s) read, {self.groups_skipped_current} node-step(s) already current, "
            f"{self.groups_skipped_unrecorded} node-step(s) kept: written before 0184; "
            f"previous versions: {versions or 'none'})"
        )


def _weights(values: Sequence[Optional[float]]) -> Optional[List[float]]:
    if not values or any(value is None for value in values):
        return None
    weights = [max(float(value or 0.0), 0.0) for value in values]
    return weights if sum(weights) > 0 else None


def container_weights(containers: Sequence[CombinedMetric]) -> Tuple[List[float], List[float]]:
    """CPU and memory usage weights of a pod's containers, as collection computes them (energy split).

    CPU: usage, else memory usage, else CPU requests, else memory requests,
    else equal. Memory: memory usage, else memory requests, else the CPU weights.
    Cost is split by reservation instead (:func:`~greenkube.impact.rows.container_cost_shares`).
    """
    cpu = (
        _weights([c.cpu_usage_millicores for c in containers])
        or _weights([c.memory_usage_bytes for c in containers])
        or _weights([c.cpu_request for c in containers])
        or _weights([c.memory_request for c in containers])
        or [1.0] * len(containers)
    )
    memory = (
        _weights([c.memory_usage_bytes for c in containers]) or _weights([c.memory_request for c in containers]) or cpu
    )
    return cpu, memory


def split_to_containers(pod: CombinedMetric, containers: Sequence[CombinedMetric]) -> List[CombinedMetric]:
    """Re-splits a re-allocated pod row onto its container rows."""
    if not containers:
        return []
    cpu_weights, _ = container_weights(containers)
    cpu_total = sum(cpu_weights)
    # Cost follows the reservation of each container, as in collection (COST-02).
    cpu_cost_shares, ram_cost_shares = container_cost_shares(
        [c.cpu_request for c in containers],
        [c.cpu_usage_millicores for c in containers],
        [c.memory_request for c in containers],
        [c.memory_usage_bytes for c in containers],
    )
    modeled = is_modeled_cost(pod.cost_source)
    result: List[CombinedMetric] = []
    for container, cpu_weight, cost_share, ram_cost_share in zip(
        containers, cpu_weights, cpu_cost_shares, ram_cost_shares, strict=True
    ):
        share = cpu_weight / cpu_total
        update: Dict[str, Any] = {
            "joules": pod.joules * share,
            "co2e_grams": pod.co2e_grams * share,
            "embodied_co2e_grams": (pod.embodied_co2e_grams or 0.0) * share,
            "model_version": pod.model_version,
            **container_gpu_fields(pod, len(containers)),
        }
        if modeled:
            cost_cpu = (pod.cost_cpu or 0.0) * cost_share
            cost_ram = (pod.cost_ram or 0.0) * ram_cost_share
            update.update(
                total_cost=cost_cpu + cost_ram,
                cost_cpu=cost_cpu,
                cost_ram=cost_ram,
                cost_storage=None,
                cost_network=None,
                cost_lb=None,
                cost_source=pod.cost_source,
                estimation_reasons=list(pod.estimation_reasons),
                is_estimated=pod.is_estimated,
            )
        result.append(container.model_copy(update=update))
    return result


@dataclass(frozen=True)
class NodeInputs:
    """Node facts of one step that the stored rows do not carry (NRG-12, NRG-13, NRG-15)."""

    host_share: float
    host_reason: Optional[str]
    node_cpu_used_cores: Optional[float]
    gpu_source: Optional[str]
    gpu_reason: Optional[str]


def _stored_reason(rows: Sequence[CombinedMetric], prefixes: Tuple[str, ...]) -> Optional[str]:
    """The first estimation reason of ``rows`` with one of ``prefixes`` (the collection's wording)."""
    for row in rows:
        for reason in row.estimation_reasons:
            if reason.startswith(prefixes):
                return reason
    return None


def _lacks_host_ids(info: Any) -> bool:
    return not getattr(info, "host_id", None) and not getattr(info, "provider_id", None)


class MetricRecomputer:
    """Re-allocates stored raw rows with the current Impact Model."""

    def __init__(
        self,
        config: Any,
        repository: Any,
        node_repository: Any,
        *,
        allocator: Optional[MetricRowAllocator] = None,
        embodied_repository: Any = None,
    ):
        self.config = config
        self.repository = repository
        self.node_repository = node_repository
        self.allocator = allocator if allocator is not None else MetricRowAllocator(config)
        self.default_profile = default_profile_from_config(config)
        lifespan_years = float(getattr(config, "DEFAULT_HARDWARE_LIFESPAN_YEARS", 4) or 0)
        self.lifespan_hours = lifespan_years * HOURS_PER_YEAR
        #: Stored Boavizta profiles (``instance_carbon_profiles``); ``None`` uses the fallbacks only.
        self.embodied_repository = embodied_repository
        self._embodied_profiles: Dict[tuple, dict] = {}
        #: Current node snapshots, read once per :meth:`run` when a pre-0183 snapshot needs them.
        self._current_nodes_cache: Optional[Mapping[str, Any]] = None

    async def run(
        self, start: datetime, end: datetime, *, only_outdated: bool = True, dry_run: bool = False
    ) -> RecomputeReport:
        report = RecomputeReport(start=start, end=end, dry_run=dry_run)
        self._current_nodes_cache = None
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + RECOMPUTE_CHUNK, end)
            await self._run_chunk(chunk_start, chunk_end, report, only_outdated=only_outdated, dry_run=dry_run)
            chunk_start = chunk_end
        return report

    async def _nodes_at(self, end: datetime) -> Mapping[str, Any]:
        try:
            snapshots = await self.node_repository.get_latest_snapshots_before(end)
        except Exception as error:
            logger.warning("Could not read node snapshots before %s: %s", end.isoformat(), error)
            return {}
        return {info.name: info for info in snapshots or [] if getattr(info, "name", None)}

    async def _current_nodes(self) -> Mapping[str, Any]:
        """The latest snapshot of every node, active or not (host ids for pre-0183 snapshots).

        Read once per :meth:`run`: the current snapshots do not change between chunks.
        """
        if self._current_nodes_cache is not None:
            return self._current_nodes_cache
        try:
            snapshots = await self.node_repository.get_latest_snapshots_before(
                datetime.now(timezone.utc), include_inactive=True
            )
        except Exception as error:
            logger.warning("Could not read the current node snapshots: %s", error)
            return {}
        self._current_nodes_cache = {info.name: info for info in snapshots or [] if getattr(info, "name", None)}
        return self._current_nodes_cache

    def _spec(
        self,
        node_name: str,
        info: Any,
        rows: Sequence[CombinedMetric],
        gpu_unattributed: float,
        inputs: Optional["NodeInputs"] = None,
    ) -> NodeSpec:
        instance_type = next((row.node_instance_type for row in rows if row.node_instance_type), None)
        instance_type = instance_type or (info.instance_type if info is not None else None)
        power = resolve_node_power_profile(
            node_name=node_name,
            instance_type=instance_type,
            cpu_capacity_cores=info.cpu_capacity_cores if info is not None else None,
            provider=info.cloud_provider if info is not None else None,
            instance_profiles=INSTANCE_PROFILES,
            default_profile=self.default_profile,
        )
        profile = power.profile
        # Same resolution as the collection (NRG-01, NRG-07): the snapshot only carries an operator
        # value; Boavizta values come from the stored profiles, never from a lookup that may fail.
        embodied = resolve_node_embodied(
            info,
            self._embodied_profiles,
            self.config,
            cpu_capacity_cores=power.known_cores,
        )
        return NodeSpec(
            name=node_name,
            instance_type=instance_type,
            provider=info.cloud_provider if info is not None else None,
            region=info.region if info is not None else None,
            cpu_capacity_cores=info.cpu_capacity_cores if info is not None else None,
            memory_capacity_bytes=info.memory_capacity_bytes if info is not None else None,
            idle_watts=float(profile.get("minWatts", 0.0) or 0.0),
            max_watts=float(profile.get("maxWatts", 0.0) or 0.0),
            vcores=float(profile.get("vcores", 0.0) or 0.0) or None,
            power_source=power.source,
            embodied_g_per_hour=embodied.g_per_hour,
            gpu_unattributed_watts=gpu_unattributed,
            is_fallback=power.is_fallback,
            embodied_source=embodied.source,
            embodied_reason=embodied.reason(node_name),
            host_share=inputs.host_share if inputs is not None else 1.0,
            host_reason=inputs.host_reason if inputs is not None else None,
            node_cpu_used_cores=inputs.node_cpu_used_cores if inputs is not None else None,
            gpu_source=inputs.gpu_source if inputs is not None else None,
            gpu_reason=inputs.gpu_reason if inputs is not None else None,
        )

    def _inputs(
        self,
        node_name: str,
        rows: Sequence[CombinedMetric],
        previous_idle: Optional[NodeIdleMetric],
        shared_hosts: Mapping[str, SharedHost],
    ) -> Optional["NodeInputs"]:
        """What the collection knew of the node at this step, or ``None`` when it cannot be re-derived.

        An idle row written since 0184 records the host share and the root-cgroup CPU the
        collection used. Without it, rows of the current model came from a collection whose
        inputs are lost (``None``: leave them as stored); older rows never had them and take the
        shared host from the node snapshots (NRG-15), with the pods' CPU sum (NRG-12).
        """
        gpu_source = previous_idle.gpu_source if previous_idle is not None else None
        gpu_reason = _stored_reason(rows, (GPU_TDP_ESTIMATE_REASON_PREFIX, GPU_UNMEASURED_REASON_PREFIX))
        shared_now = shared_hosts.get(node_name)
        if (
            previous_idle is not None
            and previous_idle.host_share is not None
            and previous_idle.host_share >= 1.0
            and shared_now is not None
            and shared_now.borrowed_ids
        ):
            # A share of 1 next to this node's snapshot without ids was written by a recompute that
            # could not group it (collections since 0183 store the ids): use the group (A.9).
            return NodeInputs(
                shared_now.share,
                shared_now.reason(node_name),
                previous_idle.node_cpu_used_cores,
                gpu_source,
                gpu_reason,
            )
        if previous_idle is not None and previous_idle.host_share is not None:
            share = previous_idle.host_share
            host_reason = _stored_reason(rows, (SHARED_HOST_REASON_PREFIX,)) if share < 1.0 else None
            if share < 1.0 and host_reason is None and node_name in shared_hosts:
                host_reason = shared_hosts[node_name].reason(node_name)
            return NodeInputs(share, host_reason, previous_idle.node_cpu_used_cores, gpu_source, gpu_reason)
        if node_name and any(row.model_version == IMPACT_MODEL_VERSION for row in rows):
            return None
        shared = shared_hosts.get(node_name)
        return NodeInputs(
            shared.share if shared is not None else 1.0,
            shared.reason(node_name) if shared is not None else None,
            None,
            gpu_source,
            gpu_reason,
        )

    async def _run_chunk(
        self,
        start: datetime,
        end: datetime,
        report: RecomputeReport,
        *,
        only_outdated: bool,
        dry_run: bool,
    ) -> None:
        pods = [row for row in await self.repository.read_combined_metrics(start, end) if not row.container_name]
        if not pods:
            return
        report.pod_rows_read += len(pods)
        containers = await self.repository.read_container_metrics(start, end)
        idle_before = {(row.node, row.timestamp): row for row in await self.repository.read_idle_metrics(start, end)}
        containers_by_pod: Dict[Tuple[str, str, Optional[datetime]], List[CombinedMetric]] = defaultdict(list)
        for container in containers:
            containers_by_pod[(container.namespace, container.pod_name, container.timestamp)].append(container)
        nodes = await self._nodes_at(end)
        self._embodied_profiles = await load_cached_profiles(self.embodied_repository, nodes, self.config)
        # Shared hosts from the snapshots' host and provider ids (0183), for rows without recorded inputs.
        # A snapshot stored before 0183 has no ids: the node's current snapshot gives them (A.9).
        shared_hosts: Mapping[str, SharedHost] = {}
        if getattr(self.config, "SHARED_HOST_DETECTION", True):
            current = await self._current_nodes() if any(_lacks_host_ids(info) for info in nodes.values()) else None
            shared_hosts = shared_host_groups(nodes, current)

        groups: Dict[Tuple[str, Optional[datetime]], List[CombinedMetric]] = defaultdict(list)
        for row in pods:
            groups[(row.node or "", row.timestamp)].append(row)

        rewritten: List[CombinedMetric] = []
        idle_rows: List[NodeIdleMetric] = []
        for (node_name, timestamp), rows in groups.items():
            if timestamp is None:
                continue
            if only_outdated and all(row.model_version == IMPACT_MODEL_VERSION for row in rows):
                report.groups_skipped_current += 1
                continue
            previous_idle = idle_before.get((node_name, timestamp))
            inputs = self._inputs(node_name, rows, previous_idle, shared_hosts)
            if inputs is None:
                report.groups_skipped_unrecorded += 1
                continue
            for row in rows:
                report.previous_versions[row.model_version] += 1
            first = rows[0]
            hours = (first.duration_seconds or 0) / 3600.0
            if hours <= 0:
                continue
            gpu_unattributed = (previous_idle.gpu_power_watts or 0.0) if previous_idle is not None else 0.0
            spec = self._spec(node_name, nodes.get(node_name), rows, gpu_unattributed, inputs)
            profile = self.allocator.node_profile(
                spec, rows, pue=first.pue or 1.0, intensity=first.grid_intensity or 0.0, hours=hours
            )
            allocated = self.allocator.allocate(
                rows,
                profile,
                hours,
                emit_idle=bool(node_name),
                idle_fields={
                    "timestamp": timestamp,
                    "emaps_zone": first.emaps_zone,
                    "intensity_source": first.intensity_source,
                },
            )
            for pod in allocated.rows:
                rewritten.append(pod)
                report.pod_rows_recomputed += 1
                split = split_to_containers(pod, containers_by_pod.get((pod.namespace, pod.pod_name, timestamp), []))
                rewritten.extend(split)
                report.container_rows_recomputed += len(split)
            if allocated.idle is not None:
                idle_rows.append(allocated.idle)

        report.idle_rows_written += len(idle_rows)
        if dry_run or not rewritten:
            return
        await self.repository.write_combined_metrics(rewritten)
        if idle_rows:
            await self.repository.write_idle_metrics(idle_rows)
