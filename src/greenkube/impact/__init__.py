# src/greenkube/impact/__init__.py
"""Impact Model v1: one versioned model for estimating and measuring impact.

See ``docs/methodology/impact-model.md`` for the methodology. Analyzers use
:class:`ImpactProjector`; storage and the ledger will use
:meth:`ImpactModel.allocate` once it is wired into collection (roadmap B2).
"""

from greenkube.impact.allocation import allocate_node, allocate_snapshot, node_totals, reservation_shares
from greenkube.impact.model import DEFAULT_IMPACT_MODEL, ImpactModel, ReservationAwareImpactModel
from greenkube.impact.power import (
    DEFAULT_MEMORY_WATTS_PER_GIB,
    GpuDevice,
    GpuPowerModel,
    LinearCpuPowerModel,
    MemoryPowerModel,
    node_gpu_power,
)
from greenkube.impact.pricing import (
    CustomRates,
    PricingCatalog,
    UnitPriceResolver,
    apportion_node_price,
    opencost_default_rates,
)
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.impact.types import (
    IDLE_WORKLOAD_NAME,
    IMPACT_MODEL_VERSION,
    AllocationResult,
    AssetDeletion,
    CapacityDelta,
    ChangeKind,
    ClusterSnapshot,
    Co2eBreakdown,
    ContainerResourceChange,
    CostBreakdown,
    CostSource,
    EnergyBreakdown,
    ImpactDelta,
    NodeImpactProfile,
    NodePoolChange,
    NodePoolContext,
    PowerSource,
    ReplicaScheduleChange,
    ResourceSample,
    UnitPrices,
    WorkloadBaseline,
    WorkloadDeletion,
    WorkloadImpact,
    WorkloadRef,
    carbon_value,
)

__all__ = [
    "AllocationResult",
    "AssetDeletion",
    "CapacityDelta",
    "ChangeKind",
    "ClusterSnapshot",
    "Co2eBreakdown",
    "ContainerResourceChange",
    "CostBreakdown",
    "CostSource",
    "CustomRates",
    "DEFAULT_IMPACT_MODEL",
    "DEFAULT_MEMORY_WATTS_PER_GIB",
    "EnergyBreakdown",
    "GpuDevice",
    "GpuPowerModel",
    "IDLE_WORKLOAD_NAME",
    "IMPACT_MODEL_VERSION",
    "ImpactDelta",
    "ImpactModel",
    "ImpactProjector",
    "LinearCpuPowerModel",
    "MemoryPowerModel",
    "NodeImpactProfile",
    "NodePoolChange",
    "NodePoolContext",
    "PowerSource",
    "PricingCatalog",
    "ReplicaScheduleChange",
    "ReservationAwareImpactModel",
    "ResourceSample",
    "UnitPriceResolver",
    "UnitPrices",
    "WorkloadBaseline",
    "WorkloadDeletion",
    "WorkloadImpact",
    "WorkloadRef",
    "allocate_node",
    "allocate_snapshot",
    "apportion_node_price",
    "carbon_value",
    "impact_fields",
    "node_gpu_power",
    "node_totals",
    "opencost_default_rates",
    "reservation_shares",
]
