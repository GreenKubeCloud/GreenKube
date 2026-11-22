"""Signals and the Source SDK (roadmap E4, E5).

Public API for source authors (see ``docs/sources.md``)::

    from greenkube.signals import (
        Capability, CollectContext, ContainerResourcesProposal, ResourceSpec, Source,
        SourceBatch, SourceHealth, SourceSettings, SourceSignal, SourceTier, TargetRef, AlgorithmInfo,
    )
"""

from greenkube.signals.model import (
    MAX_RAW_BYTES,
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    HpaProposal,
    NodePoolProposal,
    Proposal,
    ReplicasProposal,
    ResourceSpec,
    ScheduleProposal,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    TimeWindow,
)
from greenkube.signals.sdk import (
    ENTRY_POINT_GROUP,
    CollectContext,
    Diagnostic,
    EnabledMode,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceHealthStatus,
    SourceLimits,
    SourceSettings,
    SourceTier,
    WorkloadInventory,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "MAX_RAW_BYTES",
    "AdvisoryProposal",
    "AlgorithmInfo",
    "Capability",
    "CollectContext",
    "ContainerResourcesProposal",
    "DeletionProposal",
    "Diagnostic",
    "EnabledMode",
    "HpaProposal",
    "NodePoolProposal",
    "Proposal",
    "ReplicasProposal",
    "ResourceSpec",
    "ScheduleProposal",
    "SignalEvidence",
    "SignalOrigin",
    "SignalProvenance",
    "Source",
    "SourceBatch",
    "SourceDeps",
    "SourceHealth",
    "SourceHealthStatus",
    "SourceLimits",
    "SourceSettings",
    "SourceSignal",
    "SourceTier",
    "TargetRef",
    "TimeWindow",
    "WorkloadInventory",
]
