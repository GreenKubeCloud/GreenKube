# src/greenkube/impact/allocation.py
"""``ReservationAwareAllocation``: the allocation policy of Impact Model v1.

Pure functions only. For one node and one window:

* **Dynamic power** ``util × (max − min)`` is allocated by CPU **usage** share.
* **Idle power** (``min`` watts) and **embodied emissions** are allocated by CPU
  **reservation** share ``max(request, usage) / allocatable``.
* **Memory power** (``W/GiB × allocatable GiB``, model 1.1.0) is allocated by
  memory **reservation** share, like the node RAM cost.
* **GPU power** is allocated to the pod DCGM attributes each GPU to; GPUs
  attributed to no pod go to ``__idle__``.
* **Cost** is allocated by reservation share per resource: the node CPU cost by
  CPU reservation, the node RAM cost by memory reservation.
* Unreserved capacity goes to an explicit ``__idle__`` workload, so the sum of
  workload allocations plus idle equals the node totals (conservation).

When the reservations exceed allocatable capacity (usage above requests),
shares are normalized by the total reservation instead of the capacity, which
keeps conservation exact and leaves nothing for ``__idle__``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from greenkube.impact.types import (
    IDLE_WORKLOAD_NAME,
    AllocationResult,
    ClusterSnapshot,
    CostBreakdown,
    EnergyBreakdown,
    NodeImpactProfile,
    ResourceSample,
    WorkloadImpact,
    WorkloadRef,
)

#: Kind used by the synthetic idle workload reference.
IDLE_KIND = "Node"


def reservation_shares(reserved: Sequence[float], allocatable: float) -> Tuple[List[float], float]:
    """Returns each reservation's share of the node and the unreserved (idle) share.

    ``reserved`` and ``allocatable`` must use the same unit (e.g. core-hours).
    The shares and the idle share always sum to 1 when anything is allocatable
    or reserved.
    """
    clean = [max(float(value), 0.0) for value in reserved]
    total = sum(clean)
    denominator = max(float(allocatable), total)
    if denominator <= 0:
        return [0.0 for _ in clean], 1.0
    shares = [value / denominator for value in clean]
    return shares, max(1.0 - sum(shares), 0.0)


def usage_shares(used: Sequence[float]) -> List[float]:
    """Returns each consumer's share of total usage (all zero when nothing is used)."""
    clean = [max(float(value), 0.0) for value in used]
    total = sum(clean)
    if total <= 0:
        return [0.0 for _ in clean]
    return [value / total for value in clean]


@dataclass(frozen=True)
class NodeTotals:
    """Node-level totals for one window, the reference of the conservation test."""

    cost: CostBreakdown
    idle_energy_kwh: float
    dynamic_energy_kwh: float
    co2e_operational_g: float
    co2e_embodied_g: float
    memory_energy_kwh: float = 0.0
    gpu_energy_kwh: float = 0.0

    @property
    def energy_kwh(self) -> float:
        return self.idle_energy_kwh + self.dynamic_energy_kwh + self.memory_energy_kwh + self.gpu_energy_kwh


def _sample_gpu_kwh(sample: ResourceSample) -> float:
    return max(sample.gpu_watts or 0.0, 0.0) * max(sample.hours, 0.0) / 1000.0


def memory_basis_gib_hours(node: NodeImpactProfile, samples: Sequence[ResourceSample], hours: float) -> float:
    """GiB-hours the node's memory power applies to: allocatable, or the total reservation when over-committed."""
    reserved = sum(sample.mem_reserved_gib * max(sample.hours, 0.0) for sample in samples)
    return max(node.allocatable_mem * hours, reserved)


def node_cpu_used_core_hours(node: NodeImpactProfile, samples: Sequence[ResourceSample], hours: float) -> float:
    """Core-hours the node's CPU was busy: its own measure (root cgroup) or, without one, the pods' sum.

    The node measure includes kubelet, container runtime and kernel CPU (NRG-12); it never
    lowers the pods' sum.
    """
    pods = sum(max(sample.cpu_usage_cores or 0.0, 0.0) * sample.hours for sample in samples)
    measured = node.node_cpu_used_cores
    if measured is None or not measured > 0:
        return pods
    return max(float(measured) * hours, pods)


def node_totals(node: NodeImpactProfile, samples: Sequence[ResourceSample], hours: float) -> NodeTotals:
    """Computes what one node costs and emits over ``hours``."""
    cpu_used_core_hours = node_cpu_used_core_hours(node, samples, hours)
    capacity_core_hours = node.cpu_capacity_cores * hours
    utilization = min(cpu_used_core_hours / capacity_core_hours, 1.0) if capacity_core_hours > 0 else 0.0
    idle_kwh = node.idle_watts * hours / 1000.0
    dynamic_kwh = utilization * max(node.max_watts - node.idle_watts, 0.0) * hours / 1000.0
    memory_kwh = max(node.memory_watts_per_gib, 0.0) * memory_basis_gib_hours(node, samples, hours) / 1000.0
    gpu_kwh = sum(_sample_gpu_kwh(sample) for sample in samples)
    gpu_kwh += max(node.gpu_unattributed_watts, 0.0) * hours / 1000.0
    return NodeTotals(
        cost=CostBreakdown(
            cpu=node.cpu_capacity_cores * node.prices.cpu_core_hour * hours,
            ram=node.mem_capacity_gib * node.prices.ram_gib_hour * hours,
        ),
        idle_energy_kwh=idle_kwh,
        dynamic_energy_kwh=dynamic_kwh,
        memory_energy_kwh=memory_kwh,
        gpu_energy_kwh=gpu_kwh,
        co2e_operational_g=(idle_kwh + dynamic_kwh + memory_kwh + gpu_kwh) * node.pue * node.grid_intensity_g_per_kwh,
        co2e_embodied_g=node.embodied_g_per_hour * hours,
    )


def allocate_node(
    node: NodeImpactProfile,
    samples: Sequence[ResourceSample],
    hours: float,
    cluster_uid: str = "",
) -> List[WorkloadImpact]:
    """Allocates one node window to the workloads observed on it, plus ``__idle__``.

    Samples of the same workload reference are aggregated into one impact.
    """
    groups: Dict[WorkloadRef, List[ResourceSample]] = defaultdict(list)
    for sample in samples:
        groups[sample.ref].append(sample)
    refs = list(groups)

    cpu_reserved = [sum(s.cpu_reserved_cores * s.hours for s in groups[ref]) for ref in refs]
    mem_reserved = [sum(s.mem_reserved_gib * s.hours for s in groups[ref]) for ref in refs]
    cpu_used = [sum(max(s.cpu_usage_cores or 0.0, 0.0) * s.hours for s in groups[ref]) for ref in refs]

    cpu_shares, cpu_idle_share = reservation_shares(cpu_reserved, node.allocatable_cpu * hours)
    mem_shares, mem_idle_share = reservation_shares(mem_reserved, node.allocatable_mem * hours)
    # Pods get their usage share of the NODE usage; CPU spent outside pods (kubelet, runtime,
    # kernel) is the system share, charged to the idle row (NRG-12).
    node_used = node_cpu_used_core_hours(node, samples, hours)
    pods_used = sum(max(value, 0.0) for value in cpu_used)
    if node_used > pods_used > 0:
        dynamic_shares = [max(value, 0.0) / node_used for value in cpu_used]
        system_share = 1.0 - pods_used / node_used
    elif node_used > 0 and pods_used <= 0:
        dynamic_shares = [0.0 for _ in cpu_used]
        system_share = 1.0
    else:
        dynamic_shares = usage_shares(cpu_used)
        system_share = 0.0
    totals = node_totals(node, samples, hours)
    intensity = node.pue * node.grid_intensity_g_per_kwh
    provenance = {
        "allocation_policy": "reservation_aware",
        "cost_source": node.prices.source.value,
        "cost_detail": node.prices.detail,
        "power_source": node.power_source.value,
    }

    impacts: List[WorkloadImpact] = []
    for index, ref in enumerate(refs):
        group = groups[ref]
        idle_kwh = cpu_shares[index] * totals.idle_energy_kwh
        dynamic_kwh = dynamic_shares[index] * totals.dynamic_energy_kwh
        memory_kwh = mem_shares[index] * totals.memory_energy_kwh
        gpu_kwh = sum(_sample_gpu_kwh(sample) for sample in group)
        impacts.append(
            WorkloadImpact(
                ref=ref,
                node=node.name,
                hours=hours,
                replica_hours=sum(s.hours for s in group),
                cpu_core_hours_requested=sum(max(s.cpu_request_cores, 0.0) * s.hours for s in group),
                cpu_core_hours_used=cpu_used[index],
                mem_gib_hours_requested=sum(max(s.mem_request_gib, 0.0) * s.hours for s in group),
                mem_gib_hours_used=sum(max(s.mem_usage_gib or 0.0, 0.0) * s.hours for s in group),
                cost_allocated=CostBreakdown(
                    cpu=cpu_shares[index] * totals.cost.cpu,
                    ram=mem_shares[index] * totals.cost.ram,
                ),
                energy_kwh=EnergyBreakdown(
                    dynamic_cpu=dynamic_kwh, dynamic_mem=memory_kwh, idle_share=idle_kwh, gpu=gpu_kwh
                ),
                co2e_operational_g=(idle_kwh + dynamic_kwh + memory_kwh + gpu_kwh) * intensity,
                co2e_embodied_g=cpu_shares[index] * totals.co2e_embodied_g,
                scope="workload",
                provenance=provenance,
            )
        )

    # The idle workload receives unreserved idle and memory power, unattributed GPU
    # power, cost and embodied emissions, and the system share of the dynamic energy
    # (CPU used outside pods, measured at node level; 0 without a node measure).
    idle_kwh = cpu_idle_share * totals.idle_energy_kwh
    memory_idle_kwh = mem_idle_share * totals.memory_energy_kwh
    gpu_idle_kwh = max(node.gpu_unattributed_watts, 0.0) * hours / 1000.0
    system_kwh = system_share * totals.dynamic_energy_kwh
    impacts.append(
        WorkloadImpact(
            ref=WorkloadRef(cluster_uid=cluster_uid, namespace="", kind=IDLE_KIND, name=IDLE_WORKLOAD_NAME),
            node=node.name,
            hours=hours,
            replica_hours=0.0,
            cpu_core_hours_requested=0.0,
            cpu_core_hours_used=0.0,
            mem_gib_hours_requested=0.0,
            mem_gib_hours_used=0.0,
            cost_allocated=CostBreakdown(cpu=cpu_idle_share * totals.cost.cpu, ram=mem_idle_share * totals.cost.ram),
            energy_kwh=EnergyBreakdown(
                dynamic_cpu=system_kwh, idle_share=idle_kwh, dynamic_mem=memory_idle_kwh, gpu=gpu_idle_kwh
            ),
            co2e_operational_g=(idle_kwh + system_kwh + memory_idle_kwh + gpu_idle_kwh) * intensity,
            co2e_embodied_g=cpu_idle_share * totals.co2e_embodied_g,
            scope="idle",
            provenance=provenance,
        )
    )
    return impacts


def allocate_snapshot(snapshot: ClusterSnapshot) -> AllocationResult:
    """Allocates every node of a snapshot; samples on unknown nodes are returned, not dropped."""
    by_node: Dict[str, List[ResourceSample]] = defaultdict(list)
    unallocated: List[ResourceSample] = []
    for sample in snapshot.samples:
        if sample.node and sample.node in snapshot.nodes:
            by_node[sample.node].append(sample)
        else:
            unallocated.append(sample)

    impacts: List[WorkloadImpact] = []
    for name in sorted(snapshot.nodes):
        impacts.extend(allocate_node(snapshot.nodes[name], by_node.get(name, []), snapshot.hours, snapshot.cluster_uid))
    return AllocationResult(impacts=impacts, unallocated=unallocated)
