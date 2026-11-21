"""Source SDK (roadmap E5).

A **source** turns something it observes (GreenKube's own usage data, VPA
objects, a KRR report, a Kubecost API...) into :class:`SourceSignal` objects.
Built-in sources and third-party plugins implement the same contract:

* class metadata: ``id``, ``display_name``, ``tier``, ``capabilities``,
  ``version``, ``limits`` and a ``settings_model`` validated from
  ``SOURCES_CONFIG[<id>]``;
* ``discover()``: are the source's prerequisites present (a CRD, an endpoint)?
  A source that is not discovered is reported ``unavailable``, never failed;
* ``health()``: can it serve right now? ``degraded`` still collects;
* ``collect(ctx)``: return a :class:`SourceBatch` of signals, with ``complete``
  False when the result is partial.

Sources never write anything and never raise into the scan: the
:class:`~greenkube.signals.runtime.SourceRunner` bounds every call with the
source's timeouts, applies its rate limit and circuit breaker, validates its
signals and records the run. See ``docs/sources.md``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, FrozenSet, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from greenkube.signals.model import Capability, SourceSignal, TargetRef, TimeWindow

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext
    from greenkube.models.metrics import Recommendation

#: Source ids: lowercase, dotted segments (``vpa``, ``greenkube.rightsizing_cpu``, ``acme.krr``).
SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*$")
#: Prefixes reserved for built-in sources and the push API.
RESERVED_PREFIXES = ("greenkube.", "push:")
#: Entry-point group third-party sources register under.
ENTRY_POINT_GROUP = "greenkube.sources"


class SourceTier(StrEnum):
    NATIVE = "native"
    OSS = "oss"
    COMMERCIAL = "commercial"
    CLOUD = "cloud"
    CUSTOM = "custom"


class EnabledMode(StrEnum):
    """``true`` runs the source (and warns when it is not discovered), ``auto`` runs it when discovered."""

    TRUE = "true"
    FALSE = "false"
    AUTO = "auto"


class SourceHealthStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class SourceHealth(BaseModel):
    """Result of ``discover()`` or ``health()``."""

    model_config = ConfigDict(frozen=True)

    status: SourceHealthStatus = SourceHealthStatus.AVAILABLE
    reason: Optional[str] = None
    #: Version of the backing system when known (VPA recommender, KRR...).
    version: Optional[str] = None

    @classmethod
    def available(cls, reason: Optional[str] = None, version: Optional[str] = None) -> "SourceHealth":
        return cls(status=SourceHealthStatus.AVAILABLE, reason=reason, version=version)

    @classmethod
    def unavailable(cls, reason: str) -> "SourceHealth":
        return cls(status=SourceHealthStatus.UNAVAILABLE, reason=reason)

    @classmethod
    def degraded(cls, reason: str) -> "SourceHealth":
        return cls(status=SourceHealthStatus.DEGRADED, reason=reason)


class SourceLimits(BaseModel):
    """Isolation bounds the runner enforces for one source."""

    model_config = ConfigDict(frozen=True)

    #: Bound on ``discover()`` and ``health()`` together.
    probe_timeout_seconds: float = Field(30.0, gt=0)
    #: Bound on one ``collect()``; a hung source is recorded as failed.
    collect_timeout_seconds: float = Field(300.0, gt=0)
    #: Rate limit: minimum time between two collections. Scans in between skip
    #: the source and keep its last known signals.
    min_interval_seconds: float = Field(0.0, ge=0)
    #: Circuit breaker: after this many consecutive failures the source is
    #: skipped until ``circuit_cooldown_seconds`` passed since the last failure.
    failure_threshold: int = Field(3, ge=1)
    circuit_cooldown_seconds: float = Field(900.0, ge=0)
    #: Signals beyond this bound are dropped and the batch marked incomplete.
    max_signals: int = Field(50_000, ge=1)


class SourceSettings(BaseModel):
    """Base settings of every source, read from ``SOURCES_CONFIG[<id>]``.

    Sources declare their own options by subclassing this model and setting
    ``settings_model``. Unknown keys are rejected so typos fail loudly.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[EnabledMode] = None


class Diagnostic(BaseModel):
    """A note a source attaches to its batch (``12 VPAs skipped: updateMode=Auto``)."""

    model_config = ConfigDict(frozen=True)

    level: str = Field("info", pattern="^(info|warning|error)$")
    code: str
    message: str
    count: Optional[int] = None


@dataclass
class SourceDeps:
    """Dependencies handed to a source at construction."""

    config: "Config"
    cluster_uid: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


@dataclass
class WorkloadInventory:
    """Workload targets known to GreenKube for this scan (one per container)."""

    targets: List[TargetRef] = field(default_factory=list)

    def find(self, namespace: Optional[str], kind: str, name: str) -> List[TargetRef]:
        return [t for t in self.targets if t.namespace == namespace and t.kind == kind and t.name == name]


@dataclass
class CollectContext:
    """What a source receives for one collection."""

    cluster_uid: str
    #: Namespace the scan is restricted to; None for a cluster-wide scan.
    namespace: Optional[str]
    window: Optional[TimeWindow]
    deadline: datetime
    inventory: WorkloadInventory = field(default_factory=WorkloadInventory)
    #: GreenKube's analysis context (usage summaries, nodes, volumes). Native
    #: sources and v1 adapters read it; its shape is not a stable plugin API.
    optimization: Optional["OptimizationContext"] = None


@dataclass
class SourceBatch:
    """The output of one ``collect()``."""

    signals: List[SourceSignal] = field(default_factory=list)
    #: False: the result is partial, so the source's previous signals and
    #: recommendations are kept and never marked stale by this run.
    complete: bool = True
    diagnostics: List[Diagnostic] = field(default_factory=list)
    #: v1 recommendations, honoured only for built-in adapters (native analyzers,
    #: VPA) and v1 connectors passed by GreenKube itself: they feed the
    #: current recommendation pipeline unchanged. The runner drops them for every
    #: other source (see :func:`accepts_v1_recommendations`): third-party advice
    #: reaches pull requests only through signals and consensus (E6), never
    #: around them.
    recommendations: List["Recommendation"] = field(default_factory=list)


class Source(ABC):
    """Base class of every recommendation source."""

    id: ClassVar[str]
    display_name: ClassVar[str]
    tier: ClassVar[SourceTier] = SourceTier.CUSTOM
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset()
    version: ClassVar[Optional[str]] = None
    settings_model: ClassVar[type[SourceSettings]] = SourceSettings
    limits: ClassVar[SourceLimits] = SourceLimits()
    default_enabled: ClassVar[EnabledMode] = EnabledMode.AUTO
    #: v1 ``Recommendation.source`` value this source's recommendations carry,
    #: for built-in sources that feed the v1 pipeline (``greenkube``, ``vpa``).
    #: Plugins must leave it None: the registry rejects them otherwise.
    legacy_source: ClassVar[Optional[str]] = None
    #: One-line description shown by ``GET /api/v1/sources``.
    description: ClassVar[str] = ""
    #: Other source ids this built-in source may put on its signals: an exact id
    #: (the VPA connector emits ``goldilocks`` signals for Goldilocks-managed VPAs)
    #: or a prefix ending with ``:`` (``push:`` for replayed push batches).
    #: Plugins must leave it empty: the registry rejects them otherwise.
    also_emits: ClassVar[FrozenSet[str]] = frozenset()

    #: Set by the registry on every instance it creates (``builtin`` or
    #: ``plugin``); None for sources a caller builds directly. Not a plugin API.
    registry_origin: Optional[str] = None

    def __init__(self, settings: SourceSettings, deps: SourceDeps):
        self.settings = settings
        self.deps = deps

    async def discover(self) -> SourceHealth:
        """Whether the source's prerequisites exist. Default: always."""
        return SourceHealth.available()

    async def health(self) -> SourceHealth:
        """Whether the source can serve now. Called after a successful ``discover()``."""
        return SourceHealth.available()

    @abstractmethod
    async def collect(self, ctx: CollectContext) -> SourceBatch:
        """Return this source's signals for the scan."""
        raise NotImplementedError

    @property
    def source_id(self) -> str:
        return type(self).id


#: ``registry_origin`` of sources loaded from the ``greenkube.sources`` entry points.
PLUGIN_ORIGIN = "plugin"


def emits_source_id(cls: type[Source], source_id: str) -> bool:
    """Whether signals of ``cls`` may carry ``source_id`` (its own id or one of ``also_emits``)."""
    if source_id == cls.id:
        return True
    for allowed in getattr(cls, "also_emits", frozenset()) or ():
        if source_id == allowed or (allowed.endswith(":") and source_id.startswith(allowed) and source_id != allowed):
            return True
    return False


def accepts_v1_recommendations(source: Source) -> bool:
    """Whether the runner passes ``SourceBatch.recommendations`` of ``source`` to the v1 pipeline.

    Only built-in sources (which declare a ``legacy_source``) do; a plugin never
    does, whatever it declares, because its advice has had no consensus or risk
    review (FAIL CLOSED).
    """
    return type(source).legacy_source is not None and getattr(source, "registry_origin", None) != PLUGIN_ORIGIN


def validate_source_class(cls: Any, *, builtin: bool = False) -> None:
    """Raise ``ValueError`` when a class does not satisfy the SDK metadata contract."""
    if not isinstance(cls, type) or not issubclass(cls, Source):
        raise ValueError(f"{cls!r} is not a greenkube.signals.Source subclass")
    source_id = getattr(cls, "id", None)
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.match(source_id):
        raise ValueError(f"invalid source id {source_id!r}: lowercase dotted segments expected")
    if not builtin and (source_id.startswith(RESERVED_PREFIXES) or source_id == "greenkube"):
        raise ValueError(f"source id {source_id!r} uses a prefix reserved for built-in sources")
    if not builtin:
        from greenkube.signals.adapters import AnalyzerSource, RecommendationSourceAdapter

        # Fail closed: only built-in sources feed the v1 pipeline or join a v1
        # source group (whose completeness drives stale reconciliation).
        if getattr(cls, "legacy_source", None) is not None:
            raise ValueError(f"source {source_id!r} must not set legacy_source: it is reserved for built-in sources")
        if getattr(cls, "also_emits", None):
            raise ValueError(f"source {source_id!r} must not set also_emits: it is reserved for built-in sources")
        if issubclass(cls, (AnalyzerSource, RecommendationSourceAdapter)):
            raise ValueError(f"source {source_id!r} must subclass Source, not a built-in adapter")
    if not isinstance(getattr(cls, "display_name", None), str) or not cls.display_name.strip():
        raise ValueError(f"source {source_id!r} must declare a display_name")
    if not isinstance(cls.tier, SourceTier):
        raise ValueError(f"source {source_id!r} has an invalid tier {cls.tier!r}")
    capabilities = cls.capabilities
    if not capabilities or not all(isinstance(item, Capability) for item in capabilities):
        raise ValueError(f"source {source_id!r} must declare at least one Capability")
    if not isinstance(cls.limits, SourceLimits):
        raise ValueError(f"source {source_id!r} limits must be a SourceLimits")
    if not (isinstance(cls.settings_model, type) and issubclass(cls.settings_model, SourceSettings)):
        raise ValueError(f"source {source_id!r} settings_model must subclass SourceSettings")
