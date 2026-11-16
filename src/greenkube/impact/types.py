# src/greenkube/impact/types.py
"""Value types of the GreenKube Impact Model.

The methodology behind these types is documented in
``docs/methodology/impact-model.md``. Every type here is a plain, immutable
value: the model itself is a set of pure functions over them, so projections
and (later) measurements can be reproduced from their inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

# One CostSource and one PowerSource for storage provenance and the Impact
# Model: both import paths name the same enums.
from greenkube.models.provenance import CostSource, PowerSource

#: Version of the Impact Model methodology, persisted with every projection and
#: every allocated metric row. Bump the major version when an existing formula
#: changes the numbers it produces, the minor version when a new component is
#: added and existing components keep their values (1.1.0: memory and GPU power,
#: 2.0.0: projections annualize over the target's observed coverage, 3.0.0: workload
#: deletions take the pool's realization factor for their reserved parts, IMP-05, and
#: ``capacity_released`` gains ``storage_gib`` and ``load_balancers``; allocations are unchanged; 4.0.0:
#: embodied emissions from the Boavizta v1 value, an operator annotation or a per-vCPU fallback
#: instead of a flat 100 kg per node, NRG-01; projections take the collection power profile, NRG-05).
IMPACT_MODEL_VERSION = "4.0.0"

#: Name of the synthetic workload that receives unreserved node capacity.
IDLE_WORKLOAD_NAME = "__idle__"

#: Hours used to annualize window totals (365 days, matching ``SECONDS_PER_YEAR``).
HOURS_PER_YEAR = 365 * 24

#: Bytes per GiB, the RAM unit used by OpenCost prices.
BYTES_PER_GIB = 1024**3


def parse_cost_source(raw: Optional[str]) -> Tuple[CostSource, Optional[str]]:
    """Maps a stored ``cost_source`` label to the :class:`CostSource` that priced it.

    Observed costs are written by OpenCost/Kubecost collection, so a missing
    label means ``opencost``. A label that names no single source (``mixed`` on
    hourly aggregates, or an unknown label) also maps to ``opencost`` and is
    returned as the second value, so callers can record it instead of hiding it.
    """
    if not raw:
        return CostSource.OPENCOST, None
    try:
        source = CostSource(raw)
    except ValueError:
        return CostSource.OPENCOST, str(raw)
    if source == CostSource.MIXED:
        return CostSource.OPENCOST, str(raw)
    return source, None


class ChangeKind(StrEnum):
    """Kinds of change the model can project."""

    CONTAINER_RESOURCES = "container_resources"
    WORKLOAD_DELETION = "workload_deletion"
    ASSET_DELETION = "asset_deletion"
    REPLICA_SCHEDULE = "replica_schedule"
    NODE_POOL = "node_pool"


@dataclass(frozen=True)
class WorkloadRef:
    """Stable workload identity, unlike pod names it survives rollouts."""

    cluster_uid: str
    namespace: str
    kind: str
    name: str
    container: Optional[str] = None


@dataclass(frozen=True)
class UnitPrices:
    """Hourly unit prices for one node, with the tier that produced them."""

    cpu_core_hour: float
    ram_gib_hour: float
    currency: str
    source: CostSource
    #: Human-readable origin, e.g. ``aws/eu-west-3/m5.large`` or ``opencost_default``.
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpu_core_hour": self.cpu_core_hour,
            "ram_gib_hour": self.ram_gib_hour,
            "currency": self.currency,
            "source": self.source.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CostBreakdown:
    """Cost split by component, in the currency of the prices used."""

    cpu: float = 0.0
    ram: float = 0.0
    gpu: float = 0.0
    storage: float = 0.0
    network: float = 0.0
    lb: float = 0.0
    #: Observed cost that the source did not split into components.
    other: float = 0.0

    @property
    def total(self) -> float:
        return self.cpu + self.ram + self.gpu + self.storage + self.network + self.lb + self.other

    def scaled(self, factor: float) -> "CostBreakdown":
        return CostBreakdown(
            cpu=self.cpu * factor,
            ram=self.ram * factor,
            gpu=self.gpu * factor,
            storage=self.storage * factor,
            network=self.network * factor,
            lb=self.lb * factor,
            other=self.other * factor,
        )

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            cpu=self.cpu + other.cpu,
            ram=self.ram + other.ram,
            gpu=self.gpu + other.gpu,
            storage=self.storage + other.storage,
            network=self.network + other.network,
            lb=self.lb + other.lb,
            other=self.other + other.other,
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            "cpu": self.cpu,
            "ram": self.ram,
            "gpu": self.gpu,
            "storage": self.storage,
            "network": self.network,
            "lb": self.lb,
            "other": self.other,
            "total": self.total,
        }


@dataclass(frozen=True)
class EnergyBreakdown:
    """Energy split by power component, in kWh (before PUE)."""

    dynamic_cpu: float = 0.0
    dynamic_mem: float = 0.0
    idle_share: float = 0.0
    gpu: float = 0.0

    @property
    def total(self) -> float:
        return self.dynamic_cpu + self.dynamic_mem + self.idle_share + self.gpu


@dataclass(frozen=True)
class Co2eBreakdown:
    """CO2e split by origin, in grams."""

    idle_operational: float = 0.0
    dynamic_operational: float = 0.0
    embodied: float = 0.0
    #: Memory power (allocated by memory reservation), since model 1.1.0.
    memory_operational: float = 0.0
    #: GPU power attributed to the workload, since model 1.1.0.
    gpu_operational: float = 0.0

    @property
    def operational(self) -> float:
        return self.idle_operational + self.dynamic_operational + self.memory_operational + self.gpu_operational

    @property
    def total(self) -> float:
        return self.operational + self.embodied

    def scaled(self, factor: float) -> "Co2eBreakdown":
        return Co2eBreakdown(
            idle_operational=self.idle_operational * factor,
            dynamic_operational=self.dynamic_operational * factor,
            embodied=self.embodied * factor,
            memory_operational=self.memory_operational * factor,
            gpu_operational=self.gpu_operational * factor,
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            "idle_operational": self.idle_operational,
            "dynamic_operational": self.dynamic_operational,
            "memory_operational": self.memory_operational,
            "gpu_operational": self.gpu_operational,
            "embodied": self.embodied,
            "total": self.total,
        }


#: CO2e components of a projection (roadmap principle "same model for estimate
#: and measurement"). ``operational_energy`` is the dynamic CPU, memory and GPU
#: power, ``idle_share`` the node idle power allocated by CPU reservation and
#: ``embodied`` the manufacturing emissions allocated by CPU reservation.
CO2E_OPERATIONAL_ENERGY = "operational_energy"
CO2E_IDLE_SHARE = "idle_share"
CO2E_EMBODIED = "embodied"
CO2E_COMPONENTS: Tuple[str, ...] = (CO2E_OPERATIONAL_ENERGY, CO2E_IDLE_SHARE, CO2E_EMBODIED)

#: Components a measurement over stored metric rows sees: ``co2e_grams`` of a
#: row is its operational CO2e (idle share, dynamic, memory and GPU power, as
#: allocated by the Impact Model); embodied emissions are stored apart
#: (``embodied_co2e_grams``) and change measurement does not read them.
MEASURED_CO2E_COMPONENTS: Tuple[str, ...] = (CO2E_OPERATIONAL_ENERGY, CO2E_IDLE_SHARE)


def co2e_components(breakdown: Co2eBreakdown) -> Dict[str, float]:
    """Splits a CO2e breakdown into the three projection components."""
    return {
        CO2E_OPERATIONAL_ENERGY: breakdown.dynamic_operational
        + breakdown.memory_operational
        + breakdown.gpu_operational,
        CO2E_IDLE_SHARE: breakdown.idle_operational,
        CO2E_EMBODIED: breakdown.embodied,
    }


@dataclass(frozen=True)
class CapacityDelta:
    """Capacity a change releases per year (negative values mean it consumes more).

    ``storage_gib`` and ``load_balancers`` are not per year: they are the
    provisioned storage and the LoadBalancer count a deletion releases (WP-08.1).
    """

    cpu_core_hours: float = 0.0
    mem_gib_hours: float = 0.0
    replica_hours: float = 0.0
    node_hours: float = 0.0
    storage_gib: float = 0.0
    load_balancers: int = 0

    def to_dict(self) -> Dict[str, float]:
        """JSON-safe representation stored as ``impact.capacity_released``."""
        return {
            "cpu_core_hours": self.cpu_core_hours,
            "mem_gib_hours": self.mem_gib_hours,
            "replica_hours": self.replica_hours,
            "node_hours": self.node_hours,
            "storage_gib": self.storage_gib,
            "load_balancers": self.load_balancers,
        }


@dataclass(frozen=True)
class NodeImpactProfile:
    """Everything the model needs to know about one node.

    ``cpu_allocatable_cores`` is the denominator of reservation shares. Until
    kubelet allocatable is collected it equals the node CPU capacity, which is
    also what OpenCost uses to compute idle cost.
    """

    name: str
    cpu_capacity_cores: float
    mem_capacity_gib: float
    #: Node power at 0% CPU utilization (W), allocated by reservation share.
    idle_watts: float
    #: Node power at 100% CPU utilization (W).
    max_watts: float
    #: Whole-node embodied emissions amortized per hour (g CO2e / h).
    embodied_g_per_hour: float
    prices: UnitPrices
    pue: float
    grid_intensity_g_per_kwh: float
    power_source: PowerSource = PowerSource.DEFAULT_PROFILE
    cpu_allocatable_cores: Optional[float] = None
    mem_allocatable_gib: Optional[float] = None
    instance_type: Optional[str] = None
    region: Optional[str] = None
    provider: Optional[str] = None
    #: True when the profile was built from defaults because the node is unknown.
    is_fallback: bool = False
    #: Memory power per GiB of allocatable memory (``MemoryPowerModel``, B5).
    #: Value types default to 0 (no memory power); builders set the configured value.
    memory_watts_per_gib: float = 0.0
    #: Power of GPUs that DCGM attributes to no pod (idle GPUs), in W.
    gpu_unattributed_watts: float = 0.0
    #: Where ``embodied_g_per_hour`` came from (``EmbodiedSource``), and the reason when it is a fallback.
    embodied_source: Optional[str] = None
    embodied_reason: Optional[str] = None

    @property
    def allocatable_cpu(self) -> float:
        value = self.cpu_allocatable_cores if self.cpu_allocatable_cores is not None else self.cpu_capacity_cores
        return max(float(value), 0.0)

    @property
    def allocatable_mem(self) -> float:
        value = self.mem_allocatable_gib if self.mem_allocatable_gib is not None else self.mem_capacity_gib
        return max(float(value), 0.0)

    @property
    def idle_watts_per_reserved_core(self) -> float:
        cores = self.allocatable_cpu
        return self.idle_watts / cores if cores > 0 else 0.0

    @property
    def dynamic_watts_per_used_core(self) -> float:
        cores = self.cpu_capacity_cores
        return max(self.max_watts - self.idle_watts, 0.0) / cores if cores > 0 else 0.0

    @property
    def memory_watts(self) -> float:
        """Memory power of the whole node (allocatable GiB × W/GiB)."""
        return self.allocatable_mem * max(self.memory_watts_per_gib, 0.0)

    @property
    def embodied_g_per_reserved_core_hour(self) -> float:
        cores = self.allocatable_cpu
        return self.embodied_g_per_hour / cores if cores > 0 else 0.0

    @property
    def cpu_price_per_reserved_core_hour(self) -> float:
        """CPU price of one reserved core-hour (node CPU cost spread over allocatable cores)."""
        cores = self.allocatable_cpu
        if cores <= 0 or self.cpu_capacity_cores <= 0:
            return self.prices.cpu_core_hour
        return self.prices.cpu_core_hour * self.cpu_capacity_cores / cores

    @property
    def ram_price_per_reserved_gib_hour(self) -> float:
        """RAM price of one reserved GiB-hour (node RAM cost spread over allocatable GiB).

        Falls back to the plain unit price when the node memory size is unknown.
        """
        gib = self.allocatable_mem
        if gib <= 0 or self.mem_capacity_gib <= 0:
            return self.prices.ram_gib_hour
        return self.prices.ram_gib_hour * self.mem_capacity_gib / gib

    @property
    def hourly_price(self) -> float:
        """Full node price per hour implied by its unit prices."""
        return self.cpu_capacity_cores * self.prices.cpu_core_hour + self.mem_capacity_gib * self.prices.ram_gib_hour


@dataclass(frozen=True)
class ResourceSample:
    """One observed (workload, node, interval) row, in model units."""

    ref: WorkloadRef
    hours: float
    cpu_request_cores: float = 0.0
    cpu_usage_cores: Optional[float] = None
    mem_request_gib: float = 0.0
    mem_usage_gib: Optional[float] = None
    node: Optional[str] = None
    timestamp: Optional[datetime] = None
    pue: Optional[float] = None
    grid_intensity_g_per_kwh: Optional[float] = None
    #: Observed allocated cost of the sample (e.g. OpenCost ``totalCost``), when known.
    observed_cost_total: Optional[float] = None
    #: Observed per-component allocated costs, when the source splits them.
    observed_cost_cpu: Optional[float] = None
    observed_cost_ram: Optional[float] = None
    observed_cost_storage: Optional[float] = None
    observed_cost_network: Optional[float] = None
    observed_cost_lb: Optional[float] = None
    observed_cost_source: Optional[str] = None
    #: GPU power attributed to the sample by DCGM (average W over ``hours``).
    gpu_watts: Optional[float] = None

    @property
    def cpu_reserved_cores(self) -> float:
        return max(self.cpu_request_cores, self.cpu_usage_cores or 0.0, 0.0)

    @property
    def mem_reserved_gib(self) -> float:
        return max(self.mem_request_gib, self.mem_usage_gib or 0.0, 0.0)


@dataclass(frozen=True)
class ObservedCoverage:
    """How long a projection's target was observed, which sets its annualization.

    ``lookback_hours`` is the analysis window; ``observed_hours`` the span from
    the target's first observation to the end of that window (a workload that
    appeared two hours ago was observable for two hours, not for the whole
    lookback). ``basis`` says where the first observation came from:
    ``target_summary`` (the usage summary of the target), ``samples`` (the
    timestamps of raw metric rows) or ``lookback`` (unknown: the whole window).
    """

    lookback_hours: float
    observed_hours: float
    basis: str
    first_observed: Optional[datetime] = None


@dataclass(frozen=True)
class WorkloadBaseline:
    """Observed history of one workload (or container) used to project a change."""

    ref: WorkloadRef
    samples: Sequence[ResourceSample]
    #: Observation window length; window totals are annualized by HOURS_PER_YEAR / window_hours.
    window_hours: float
    #: Observed coverage of the target. When set, the model annualizes over it
    #: (with a minimum coverage) instead of ``window_hours`` and reports it.
    coverage: Optional[ObservedCoverage] = None


@dataclass(frozen=True)
class ContainerResourceChange:
    """Change of container requests: ``(current, proposed)`` per resource."""

    cpu_request_cores: Optional[Tuple[float, float]] = None
    mem_request_gib: Optional[Tuple[float, float]] = None

    kind = ChangeKind.CONTAINER_RESOURCES


@dataclass(frozen=True)
class WorkloadDeletion:
    """Removal of every replica of a workload (zombie pods, idle namespaces)."""

    kind = ChangeKind.WORKLOAD_DELETION


@dataclass(frozen=True)
class AssetDeletion:
    """Removal of a non-compute asset whose allocated rate is known (PV, LoadBalancer)."""

    asset_kind: str
    allocated_cost_annual: float
    #: Cost component the asset belongs to: ``storage``, ``network`` or ``lb``.
    component: str
    cost_source: CostSource
    detail: str = ""
    #: Provisioned storage the deletion releases (PersistentVolume), in GiB.
    storage_gib: float = 0.0
    #: LoadBalancers the deletion releases.
    load_balancers: int = 0

    kind = ChangeKind.ASSET_DELETION


@dataclass(frozen=True)
class ReplicaScheduleChange:
    """Scale a workload to zero during the given UTC hours of the day."""

    removed_hours_of_day: frozenset[int]

    kind = ChangeKind.REPLICA_SCHEDULE


@dataclass(frozen=True)
class NodePoolChange:
    """Removal of whole nodes from a pool (consolidation)."""

    removed_nodes: Tuple[str, ...]
    #: True when the recommended action is the consolidation itself (realization 1).
    action_is_consolidation: bool = True

    kind = ChangeKind.NODE_POOL


ProposedChange = Union[
    ContainerResourceChange,
    WorkloadDeletion,
    AssetDeletion,
    ReplicaScheduleChange,
    NodePoolChange,
]


@dataclass(frozen=True)
class NodePoolContext:
    """Nodes, prices and realization inputs used for one projection run."""

    nodes: Mapping[str, NodeImpactProfile]
    default_node: NodeImpactProfile
    realization_factor: float
    carbon_price_per_tonne: float
    currency: str
    #: Where the realization factor comes from: ``config:IMPACT_REALIZATION_FACTOR``,
    #: ``realization_context`` (B6) or ``realization_context:unobserved``.
    realization_source: str = "config"
    #: Why the factor has its value (rendered with the projection).
    realization_reasons: Tuple[str, ...] = ()
    #: Per-pool realization observed in the cluster (``greenkube.impact.realization``);
    #: None means ``realization_factor`` applies to every node.
    realization: Optional[Any] = None

    def node_for(self, name: Optional[str]) -> NodeImpactProfile:
        if name and name in self.nodes:
            return self.nodes[name]
        return self.default_node

    def realization_for(self, samples: Sequence["ResourceSample"]) -> Tuple[float, str, Tuple[str, ...], Tuple]:
        """Realization factor, source, reasons and pools for the nodes ``samples`` ran on.

        With an observed context the factor is the hours-weighted factor of the
        samples' node pools; otherwise the pool-wide ``realization_factor``.
        """
        if self.realization is None:
            return self.realization_factor, self.realization_source, self.realization_reasons, ()
        from greenkube.impact.realization import realization_for_nodes

        weights: Dict[Optional[str], float] = {}
        for sample in samples:
            weights[sample.node] = weights.get(sample.node, 0.0) + max(sample.hours, 0.0)
        resolved = realization_for_nodes(self.realization, weights)
        return resolved.factor, resolved.source, resolved.reasons, resolved.pools


@dataclass(frozen=True)
class ImpactDelta:
    """Expected effect of a proposed change, per year.

    ``allocated_*`` is what the workload stops being charged under the
    reservation-aware allocation; ``expected_*`` multiplies it by the
    realization factor (how much of the released capacity turns into removed
    billable node-hours). Projected values are never measured values.
    """

    kind: ChangeKind
    allocated_cost_annual: float
    cost_breakdown_annual: CostBreakdown
    capacity_released: CapacityDelta
    expected_billable_cost_annual: float
    realization_factor: float
    co2e_allocated_annual_g: float
    co2e_expected_annual_g: float
    co2e_breakdown_annual_g: Co2eBreakdown
    #: False when the change affects something the energy model does not cover (storage, LBs).
    co2e_modeled: bool
    carbon_price_per_tonne: float
    #: ``expected_billable_cost_annual + carbon_price × co2e_expected_annual_g / 1e6`` (I1/I2).
    value_annual: float
    currency: str
    confidence: float
    assumptions: Dict[str, Any] = field(default_factory=dict)
    model_version: str = IMPACT_MODEL_VERSION
    #: Expected CO2e per component when the factor differs between components (a workload
    #: deletion keeps its usage-based power whole, IMP-05); ``None``: every component x the factor.
    co2e_expected_breakdown_annual_g: Optional[Co2eBreakdown] = None

    @property
    def co2e_components_annual_g(self) -> Dict[str, float]:
        """Allocated annual CO2e by projection component (operational energy, idle share, embodied)."""
        return co2e_components(self.co2e_breakdown_annual_g)

    @property
    def co2e_measurable_expected_annual_g(self) -> Optional[float]:
        """Expected annual CO2e of the components a change measurement can see (None when not modeled)."""
        if not self.co2e_modeled:
            return None
        if self.co2e_expected_breakdown_annual_g is not None:
            expected = co2e_components(self.co2e_expected_breakdown_annual_g)
            return sum(expected[name] for name in MEASURED_CO2E_COMPONENTS)
        components = self.co2e_components_annual_g
        return sum(components[name] for name in MEASURED_CO2E_COMPONENTS) * self.realization_factor

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe representation persisted as ``impact`` on recommendations."""
        return {
            "model_version": self.model_version,
            "kind": self.kind.value,
            "method": "projected",
            "currency": self.currency,
            "allocated_cost_annual": self.allocated_cost_annual,
            "cost_breakdown_annual": self.cost_breakdown_annual.to_dict(),
            "capacity_released": self.capacity_released.to_dict(),
            "expected_billable_cost_annual": self.expected_billable_cost_annual,
            "realization_factor": self.realization_factor,
            "co2e_modeled": self.co2e_modeled,
            "co2e_allocated_annual_g": self.co2e_allocated_annual_g,
            "co2e_expected_annual_g": self.co2e_expected_annual_g,
            "co2e_breakdown_annual_g": self.co2e_breakdown_annual_g.to_dict(),
            "co2e_components_annual_g": self.co2e_components_annual_g,
            "co2e_measurable_components": list(MEASURED_CO2E_COMPONENTS),
            "co2e_measurable_expected_annual_g": self.co2e_measurable_expected_annual_g,
            **(
                {"co2e_expected_breakdown_annual_g": self.co2e_expected_breakdown_annual_g.to_dict()}
                if self.co2e_expected_breakdown_annual_g is not None
                else {}
            ),
            "carbon_price_per_tonne": self.carbon_price_per_tonne,
            "value_annual": self.value_annual,
            "confidence": self.confidence,
            "assumptions": dict(self.assumptions),
        }


@dataclass(frozen=True)
class WorkloadImpact:
    """Allocation of one node interval to one workload (or to ``__idle__``)."""

    ref: WorkloadRef
    node: str
    hours: float
    replica_hours: float
    cpu_core_hours_requested: float
    cpu_core_hours_used: float
    mem_gib_hours_requested: float
    mem_gib_hours_used: float
    cost_allocated: CostBreakdown
    energy_kwh: EnergyBreakdown
    co2e_operational_g: float
    co2e_embodied_g: float
    #: ``workload`` for real workloads, ``idle`` for the unreserved remainder.
    scope: str = "workload"
    provenance: Mapping[str, str] = field(default_factory=dict)
    model_version: str = IMPACT_MODEL_VERSION


@dataclass(frozen=True)
class ClusterSnapshot:
    """Inputs of one allocation window: node profiles and the samples observed on them."""

    cluster_uid: str
    hours: float
    nodes: Mapping[str, NodeImpactProfile]
    samples: Sequence[ResourceSample]


@dataclass(frozen=True)
class AllocationResult:
    """Output of :meth:`ImpactModel.allocate`."""

    impacts: Sequence[WorkloadImpact]
    #: Samples whose node is not part of the snapshot; never silently dropped.
    unallocated: Sequence[ResourceSample] = ()


def carbon_value(cost: float, co2e_grams: float, carbon_price_per_tonne: float) -> float:
    """Unifies cost and carbon into one currency amount (roadmap B, I1)."""
    return cost + carbon_price_per_tonne * co2e_grams / 1e6
