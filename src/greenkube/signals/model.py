"""Signal model v2 (roadmap E4).

A **signal** is one source's proposal for one target and one capability. Every
source emits signals in this schema, GreenKube's own analyzers included:

* ``target`` is a canonical, cluster-qualified :class:`TargetRef`;
* ``capability`` is the optimization domain (:class:`Capability`), coarser than
  the v1 recommendation type, which travels as ``kind``;
* ``proposal`` is a typed change (:data:`Proposal`), discriminated by ``kind``;
* ``evidence``, ``observed_window``, ``algorithm`` and ``confidence`` explain why
  the source proposes it; ``source_estimate`` keeps the savings the source
  claims, for comparison only (GreenKube's Impact Model owns the numbers);
* ``provenance`` records where the signal came from (native analyzer, adapter
  over a v1 connector, third-party plugin).

``signal_id`` is derived: ``sha256(source_id, target.key(), capability, kind,
proposal identity)``. The proposal identity is the proposal without the fields
that describe the observed state or explain the change (``current``,
``direction``, ``reason``): they drift between scans without changing the
advice. The same proposal from the same source on the same target keeps the
same id across scans, so storage keeps one row per distinct proposal (holding
its latest observed state and reason) instead of one row per scan.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Dict, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Version of the signal schema written to storage.
SIGNAL_SCHEMA_VERSION = 1
#: Upper bound on a signal's trimmed raw payload (JSON bytes).
MAX_RAW_BYTES = 8 * 1024

#: API version of the Kubernetes kinds GreenKube targets.
KIND_API_VERSIONS: Dict[str, str] = {
    "Deployment": "apps/v1",
    "StatefulSet": "apps/v1",
    "DaemonSet": "apps/v1",
    "ReplicaSet": "apps/v1",
    "CronJob": "batch/v1",
    "Job": "batch/v1",
    "Rollout": "argoproj.io/v1alpha1",
    "HorizontalPodAutoscaler": "autoscaling/v2",
    "NodePool": "karpenter.sh/v1",
    "PersistentVolume": "v1",
    "PersistentVolumeClaim": "v1",
    "Service": "v1",
    "Namespace": "v1",
    "Node": "v1",
    "Pod": "v1",
}

#: Kinds that are not namespaced: their ``TargetRef.namespace`` is always None.
CLUSTER_SCOPED_KINDS = frozenset({"Namespace", "Node", "NodePool", "PersistentVolume", "Cluster"})


class Capability(StrEnum):
    """Optimization domain of a signal (v2). One consolidated recommendation per target and capability."""

    CONTAINER_REQUESTS = "container_requests"
    CONTAINER_LIMITS = "container_limits"
    REPLICAS = "replicas"
    HORIZONTAL_AUTOSCALING = "horizontal_autoscaling"
    #: Off-peak / sleep windows.
    SCHEDULE = "schedule"
    #: Orphaned PV/LB, zombie workloads, idle namespaces.
    CLEANUP = "cleanup"
    NODE_POOL = "node_pool"
    #: Carbon-aware placement.
    PLACEMENT = "placement"
    #: Missing requests/limits and other best-practice findings.
    HYGIENE = "hygiene"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TargetRef(_Frozen):
    """Canonical, cluster-qualified identity of the object a signal is about."""

    cluster_uid: str = Field(..., min_length=1)
    api_version: str = ""
    kind: str = Field(..., min_length=1)
    namespace: Optional[str] = None
    name: str = Field(..., min_length=1)
    container: Optional[str] = None

    @classmethod
    def for_kind(
        cls,
        cluster_uid: str,
        kind: str,
        name: str,
        *,
        namespace: Optional[str] = None,
        container: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> "TargetRef":
        """Build a ref, filling the API version of well-known kinds."""
        return cls(
            cluster_uid=cluster_uid,
            api_version=api_version if api_version is not None else KIND_API_VERSIONS.get(kind, ""),
            kind=kind,
            namespace=None if kind in CLUSTER_SCOPED_KINDS else namespace,
            name=name,
            container=container or None,
        )

    @property
    def group(self) -> str:
        """API group of the kind (``""`` for the core group)."""
        return self.api_version.split("/", 1)[0] if "/" in self.api_version else ""

    def key(self) -> str:
        """``cluster/apps.Deployment/ns/name[/container]``; ``_`` stands for no namespace."""
        kind = f"{self.group}.{self.kind}" if self.group else self.kind
        key = f"{self.cluster_uid}/{kind}/{self.namespace or '_'}/{self.name}"
        return f"{key}/{self.container}" if self.container else key


class ResourceSpec(_Frozen):
    """Container resources; None means unknown or unset."""

    cpu_request_m: Optional[int] = Field(None, ge=0)
    memory_request_bytes: Optional[int] = Field(None, ge=0)
    cpu_limit_m: Optional[int] = Field(None, ge=0)
    memory_limit_bytes: Optional[int] = Field(None, ge=0)


class ContainerResourcesProposal(_Frozen):
    """Change container requests (or limits) from ``current`` to ``proposed``."""

    kind: Literal["container_resources"] = "container_resources"
    container: Optional[str] = None
    current: ResourceSpec = ResourceSpec()
    proposed: ResourceSpec = ResourceSpec()
    direction: Literal["decrease", "increase", "mixed", "unchanged"] = "unchanged"

    @model_validator(mode="after")
    def _derive_direction(self) -> "ContainerResourcesProposal":
        object.__setattr__(self, "direction", resource_direction(self.current, self.proposed))
        return self


class ReplicasProposal(_Frozen):
    kind: Literal["replicas"] = "replicas"
    current: Optional[int] = Field(None, ge=0)
    proposed: int = Field(..., ge=0)


class HpaProposal(_Frozen):
    """Add or tune horizontal autoscaling. Unknown bounds stay None: a source never invents them."""

    kind: Literal["hpa"] = "hpa"
    min_replicas: Optional[int] = Field(None, ge=0)
    max_replicas: Optional[int] = Field(None, ge=1)
    target_cpu_utilization: Optional[int] = Field(None, ge=1, le=100)
    current: Optional[ResourceSpec] = None


class ScheduleProposal(_Frozen):
    """Scale down during a recurring window (``sleep_at``/``wake_at`` are ``HH:MM``)."""

    kind: Literal["schedule"] = "schedule"
    timezone: str = "UTC"
    sleep_at: str
    wake_at: str
    #: Cron day-of-week field; ``*`` is every day.
    weekdays: str = "*"
    replicas_while_asleep: int = Field(0, ge=0)


class DeletionProposal(_Frozen):
    """Delete the target. Never automated in this release (FAIL CLOSED): review only."""

    kind: Literal["deletion"] = "deletion"
    reason: str = ""


class NodePoolProposal(_Frozen):
    """JSON merge patch on a NodePool spec."""

    kind: Literal["node_pool"] = "node_pool"
    patch: Dict[str, Any] = Field(default_factory=dict)


class AdvisoryProposal(_Frozen):
    """Advice that cannot become a pull request."""

    kind: Literal["advisory"] = "advisory"
    text: str = ""


Proposal = Annotated[
    Union[
        ContainerResourcesProposal,
        ReplicasProposal,
        HpaProposal,
        ScheduleProposal,
        DeletionProposal,
        NodePoolProposal,
        AdvisoryProposal,
    ],
    Field(discriminator="kind"),
]


class TimeWindow(_Frozen):
    """Half-open observation window ``[start, end)``."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "TimeWindow":
        if self.end < self.start:
            raise ValueError("window end must not precede its start")
        return self


class AlgorithmInfo(_Frozen):
    """How the source computed its proposal (``vpa-recommender``, ``greenkube.rightsizing_cpu``...)."""

    name: str = Field(..., min_length=1)
    params: Dict[str, Any] = Field(default_factory=dict)
    window: Optional[TimeWindow] = None


class SignalEvidence(_Frozen):
    """Why the source proposes the change: a summary, its reasoning and structured data."""

    summary: str = ""
    details: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class SignalOrigin(StrEnum):
    #: A GreenKube analyzer.
    NATIVE = "native"
    #: A v1 ``RecommendationSource`` connector wrapped by the SDK adapter.
    ADAPTER = "adapter"
    #: A third-party source loaded from the ``greenkube.sources`` entry points.
    PLUGIN = "plugin"
    #: Pushed through the signals API (G-wave).
    PUSH = "push"


class SignalProvenance(_Frozen):
    """Where a signal comes from and how it maps back to the v1 recommendation API.

    ``legacy_source`` is the v1 ``Recommendation.source`` value the signal maps
    to (``greenkube``, ``vpa``, ``karpenter``); None for sources that only emit
    signals. ``legacy_fields`` holds the v1 fields that cannot be derived from
    the target and the proposal (transitional, dropped with the v1 API).
    ``legacy_fingerprint`` is the identity of the v1 recommendation the signal
    supports; signals of different sources on the same issue share it.
    """

    origin: SignalOrigin = SignalOrigin.NATIVE
    legacy_source: Optional[str] = None
    legacy_fingerprint: Optional[str] = None
    legacy_fields: Dict[str, Any] = Field(default_factory=dict)


def resource_direction(current: ResourceSpec, proposed: ResourceSpec) -> str:
    """``decrease``, ``increase``, ``mixed`` or ``unchanged`` over the resources both sides know."""
    directions = set()
    for name in ResourceSpec.model_fields:
        before, after = getattr(current, name), getattr(proposed, name)
        if before is None or after is None or before == after:
            continue
        directions.add("decrease" if after < before else "increase")
    if not directions:
        return "unchanged"
    return directions.pop() if len(directions) == 1 else "mixed"


#: Resources a container-resources proposal must carry a value for, per capability.
CAPABILITY_RESOURCES: Dict[str, Tuple[str, ...]] = {
    "container_requests": ("cpu_request_m", "memory_request_bytes"),
    "container_limits": ("cpu_limit_m", "memory_limit_bytes"),
}


#: Finding kinds whose consensus groups compare numeric request or limit values (``consensus`` groups).
RESOURCE_VALUE_KINDS = frozenset({"RIGHTSIZING_CPU", "RIGHTSIZING_MEMORY", "cpu_limit_m", "memory_limit_bytes"})


def missing_resource_value(capability: Any, proposal: Any, kind: Optional[str] = None) -> Optional[str]:
    """Why a container requests or limits signal carries no value for its capability, or None.

    * A ``container_resources`` proposal under ``container_requests`` must
      propose a CPU or memory request, under ``container_limits`` a CPU or
      memory limit.
    * A signal of a request or limit finding kind (``RIGHTSIZING_CPU``,
      ``RIGHTSIZING_MEMORY``...) must carry a ``container_resources`` proposal:
      an advisory, replicas or other proposal would join that numeric group,
      be compared by identity and lose every source's value (and the native
      OOM floor).

    Advice of another kind (a pushed ``WORKLOAD_UNDERPROVISIONED`` advisory) and
    other capabilities pass.
    """
    name = str(getattr(capability, "value", capability))
    resources = CAPABILITY_RESOURCES.get(name)
    if resources is None:
        return None
    if not isinstance(proposal, ContainerResourcesProposal):
        if kind in RESOURCE_VALUE_KINDS:
            shape = getattr(proposal, "kind", type(proposal).__name__)
            return f"a {name} signal of kind {kind} needs a container_resources proposal, not {shape}"
        return None
    if all(getattr(proposal.proposed, resource) is None for resource in resources):
        return f"a {name} signal must propose a value for {resources[0]} or {resources[1]}"
    return None


#: Proposal fields outside a signal's identity: the observed state (``current``
#: and the ``direction`` derived from it) and free-text explanations (``reason``).
NON_IDENTITY_PROPOSAL_FIELDS = frozenset({"current", "direction", "reason"})


def proposal_identity(proposal: BaseModel) -> Dict[str, Any]:
    """The part of a proposal that identifies the advice (what to change, to what)."""
    data = proposal.model_dump(mode="json")
    return {key: value for key, value in data.items() if key not in NON_IDENTITY_PROPOSAL_FIELDS}


def compute_signal_id(source_id: str, target: TargetRef, capability: str, proposal: BaseModel, kind: str = "") -> str:
    """Deterministic identity of one source's proposal on one target and capability."""
    payload = json.dumps(
        [source_id, target.key(), str(capability), kind, proposal_identity(proposal)],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def raw_size(raw: Dict[str, Any]) -> int:
    """Size of a raw payload in JSON bytes."""
    return len(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def trim_raw(raw: Dict[str, Any], limit: int = MAX_RAW_BYTES) -> Dict[str, Any]:
    """Keep ``raw`` when it fits ``limit`` bytes, else a marker with its size."""
    size = raw_size(raw)
    if size <= limit:
        return raw
    return {"truncated": True, "size_bytes": size}


class SourceSignal(BaseModel):
    """One source's proposal for one target and capability (roadmap E4)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SIGNAL_SCHEMA_VERSION
    signal_id: str = ""
    source_id: str = Field(..., min_length=1)
    source_version: Optional[str] = None
    #: Connector-specific reference (``ns/vpa-name``, a KRR run id...).
    source_ref: Optional[str] = None
    #: Recommendation engine behind the proposal when several sources share one
    #: (``vpa`` for VPA and Goldilocks VPAs). Consensus counts one vote per engine
    #: family; None means the source is its own engine (see :attr:`engine`).
    engine_family: Optional[str] = Field(None, min_length=1, max_length=64)
    target: TargetRef
    capability: Capability
    #: Finding kind; GreenKube sources use the v1 recommendation type (``RIGHTSIZING_CPU``...).
    kind: str = Field(..., min_length=1)
    proposal: Proposal
    algorithm: AlgorithmInfo
    evidence: SignalEvidence = SignalEvidence()
    observed_window: Optional[TimeWindow] = None
    #: The source's own confidence in its proposal, 0..1, when it reports one.
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    #: Savings claimed by the source, kept for comparison only.
    source_estimate: Optional[Dict[str, Any]] = None
    provenance: SignalProvenance = SignalProvenance()
    #: Trimmed raw payload for audit (at most :data:`MAX_RAW_BYTES`; the runner trims it).
    raw: Dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("fetched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def _derive_id(self) -> "SourceSignal":
        self.signal_id = compute_signal_id(self.source_id, self.target, self.capability, self.proposal, self.kind)
        return self

    @property
    def target_key(self) -> str:
        return self.target.key()

    @property
    def engine(self) -> str:
        """Engine family consensus votes by: ``engine_family``, else the source id."""
        return self.engine_family or self.source_id
