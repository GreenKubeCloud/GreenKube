"""SDK adapters over GreenKube's v1 recommendation producers (roadmap E5).

* :class:`AnalyzerSource` runs one native analyzer. :func:`analyzer_source_class`
  builds one source class per analyzer (``greenkube.rightsizing_cpu``,
  ``greenkube.orphaned_lb``...), so each analyzer has its own health, run
  history and error isolation, while its internals stay untouched.
* :class:`RecommendationSourceAdapter` wraps a v1
  :class:`~greenkube.core.optimization.providers.base.RecommendationSource`
  connector (VPA, the Karpenter NodePool heuristic, test fakes).

Both keep emitting the v1 recommendations unchanged (``SourceBatch.recommendations``)
and add their v2 signals, so the current recommendation output is identical.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Type

from greenkube.signals.mapping import signal_from_recommendation
from greenkube.signals.model import AlgorithmInfo, Capability, SignalOrigin, SourceSignal, TargetRef, TimeWindow
from greenkube.signals.sdk import (
    CollectContext,
    Diagnostic,
    Source,
    SourceBatch,
    SourceDeps,
    SourceHealth,
    SourceSettings,
    SourceTier,
    WorkloadInventory,
)

if TYPE_CHECKING:
    from greenkube.core.optimization.analyzers.base import Analyzer
    from greenkube.core.optimization.context import OptimizationContext
    from greenkube.core.optimization.providers.base import RecommendationSource
    from greenkube.models.metrics import Recommendation

#: Source id of each built-in analyzer (by class name). Unknown analyzers get
#: ``greenkube.<snake_case name without "Analyzer">``.
NATIVE_SOURCE_IDS: Dict[str, str] = {
    "ZombieAnalyzer": "greenkube.zombie",
    "CpuRightsizingAnalyzer": "greenkube.rightsizing_cpu",
    "MemoryRightsizingAnalyzer": "greenkube.rightsizing_memory",
    "AutoscalingAnalyzer": "greenkube.autoscaling",
    "OffPeakAnalyzer": "greenkube.off_peak",
    "IdleNamespaceAnalyzer": "greenkube.idle_namespace",
    "CarbonAwareAnalyzer": "greenkube.carbon_aware",
    "NodesAnalyzer": "greenkube.nodes",
    "OrphanedPVAnalyzer": "greenkube.orphaned_pv",
    "OrphanedLBAnalyzer": "greenkube.orphaned_lb",
}

#: v2 capability of each v1 analyzer capability.
_ANALYZER_CAPABILITIES: Dict[str, Capability] = {
    "cpu_rightsizing": Capability.CONTAINER_REQUESTS,
    "memory_rightsizing": Capability.CONTAINER_REQUESTS,
    "autoscaling": Capability.HORIZONTAL_AUTOSCALING,
    "off_peak": Capability.SCHEDULE,
    "carbon_aware": Capability.PLACEMENT,
    "zombie_cleanup": Capability.CLEANUP,
    "namespace_cleanup": Capability.CLEANUP,
    "storage_cleanup": Capability.CLEANUP,
    "lb_cleanup": Capability.CLEANUP,
    "node_optimization": Capability.NODE_POOL,
    "node_pool": Capability.NODE_POOL,
}


def native_source_id(analyzer_cls: type) -> str:
    """Source id of a native analyzer class."""
    name = analyzer_cls.__name__
    if name in NATIVE_SOURCE_IDS:
        return NATIVE_SOURCE_IDS[name]
    stem = name[: -len("Analyzer")] if name.endswith("Analyzer") and name != "Analyzer" else name
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()
    return f"greenkube.{snake}"


def observed_window(context: Optional["OptimizationContext"]) -> Optional[TimeWindow]:
    """The analysis window of a scan, when the context knows it."""
    if context is None or context.window_start is None or context.window_end is None:
        return None
    try:
        return TimeWindow(start=context.window_start, end=context.window_end)
    except ValueError:
        return None


def workload_inventory(context: Optional["OptimizationContext"], cluster_uid: str) -> WorkloadInventory:
    """Workload targets (one per known container) of a scan's usage snapshot."""
    if context is None:
        return WorkloadInventory()
    targets: List[TargetRef] = []
    try:
        for (namespace, kind, name), container, _summary in context.iter_container_summaries():
            if not name or not kind:
                continue
            targets.append(TargetRef.for_kind(cluster_uid, kind, name, namespace=namespace, container=container))
    except Exception:
        return WorkloadInventory()
    return WorkloadInventory(targets=targets)


def _require_context(ctx: CollectContext, source_id: str) -> "OptimizationContext":
    if ctx.optimization is None:
        raise RuntimeError(f"source {source_id!r} needs GreenKube's optimization context")
    return ctx.optimization


def _signals(
    recommendations: List["Recommendation"],
    *,
    source: Source,
    ctx: CollectContext,
    algorithm: AlgorithmInfo,
    origin: SignalOrigin,
    fetched_at: datetime,
    raw_for: Any = None,
) -> List[SourceSignal]:
    window = ctx.window
    return [
        signal_from_recommendation(
            rec,
            source_id=source.source_id,
            source_version=type(source).version,
            cluster_uid=ctx.cluster_uid,
            fetched_at=fetched_at,
            algorithm=algorithm,
            observed_window=window,
            origin=origin,
            raw=raw_for(rec) if raw_for is not None else None,
        )
        for rec in recommendations
    ]


class AnalyzerSource(Source):
    """One native GreenKube analyzer exposed as a source."""

    tier: ClassVar[SourceTier] = SourceTier.NATIVE
    legacy_source: ClassVar[Optional[str]] = "greenkube"
    analyzer_class: ClassVar[Type["Analyzer"]]

    def __init__(self, settings: SourceSettings, deps: SourceDeps):
        super().__init__(settings, deps)
        self.analyzer = type(self).analyzer_class(deps.config)

    def _algorithm(self, ctx: CollectContext) -> AlgorithmInfo:
        config = self.deps.config
        return AlgorithmInfo(
            name=self.source_id,
            params={"lookback_days": getattr(config, "RECOMMENDATION_LOOKBACK_DAYS", None)},
            window=ctx.window,
        )

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        context = _require_context(ctx, self.source_id)
        recommendations = list(self.analyzer.analyze(context))
        signals = _signals(
            recommendations,
            source=self,
            ctx=ctx,
            algorithm=self._algorithm(ctx),
            origin=SignalOrigin.NATIVE,
            fetched_at=self.deps.clock(),
        )
        diagnostics = [Diagnostic(**note) for note in self.analyzer.scan_diagnostics()]
        return SourceBatch(
            signals=signals,
            recommendations=recommendations,
            complete=self.analyzer.scan_complete(),
            diagnostics=diagnostics,
        )


def analyzer_source_class(analyzer_cls: Type["Analyzer"]) -> Type[AnalyzerSource]:
    """Build the source class of one native analyzer."""
    v1_capability = str(getattr(analyzer_cls, "capability", "") or "")
    capability = _ANALYZER_CAPABILITIES.get(v1_capability, Capability.HYGIENE)
    source_id = native_source_id(analyzer_cls)
    doc = (analyzer_cls.__doc__ or "").strip().splitlines()
    attrs: Dict[str, Any] = {
        "id": source_id,
        "display_name": f"GreenKube {source_id.split('.', 1)[1].replace('_', ' ')}",
        "capabilities": frozenset({capability}),
        "analyzer_class": analyzer_cls,
        "description": doc[0] if doc else f"Native analyzer {analyzer_cls.__name__}.",
        "__module__": __name__,
    }
    return type(f"{analyzer_cls.__name__}Source", (AnalyzerSource,), attrs)


class RecommendationSourceAdapter(Source):
    """A v1 ``RecommendationSource`` connector exposed as a source.

    Subclasses implement ``build_connector`` and set the SDK metadata;
    :func:`adapt_recommendation_source` wraps an existing connector instance
    (for tests and embedding callers).
    """

    tier: ClassVar[SourceTier] = SourceTier.CUSTOM
    #: Algorithm reported on the signals (``vpa-recommender``...); defaults to the source id.
    algorithm_name: ClassVar[Optional[str]] = None
    #: ``native`` for GreenKube heuristics wrapped as connectors (NodePool).
    signal_origin: ClassVar[SignalOrigin] = SignalOrigin.ADAPTER

    def __init__(
        self,
        settings: SourceSettings,
        deps: SourceDeps,
        connector: Optional["RecommendationSource"] = None,
    ):
        super().__init__(settings, deps)
        self.connector = connector if connector is not None else self.build_connector(deps)

    def build_connector(self, deps: SourceDeps) -> "RecommendationSource":
        raise NotImplementedError(f"{type(self).__name__} needs a connector")

    async def discover(self) -> SourceHealth:
        if await self.connector.is_available():
            return SourceHealth.available()
        return SourceHealth.unavailable(f"{type(self).display_name} is not installed or not reachable")

    def raw_payload(self, rec: "Recommendation") -> Optional[Dict[str, Any]]:
        """Trimmed audit payload of one recommendation; subclasses add the connector's view."""
        return None

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        context = _require_context(ctx, self.source_id)
        _reset_completeness(self.connector)
        recommendations = list(await self.connector.collect(context))
        complete = getattr(self.connector, "last_collect_complete", True) is not False
        algorithm = AlgorithmInfo(name=type(self).algorithm_name or self.source_id, window=ctx.window)
        signals = _signals(
            recommendations,
            source=self,
            ctx=ctx,
            algorithm=algorithm,
            origin=type(self).signal_origin,
            fetched_at=self.deps.clock(),
            raw_for=self.raw_payload,
        )
        return SourceBatch(signals=signals, recommendations=recommendations, complete=complete)


def _reset_completeness(connector: Any) -> None:
    """Clear a v1 connector's partial-result flag before each ``collect()`` call.

    The flag describes one call; without the reset a single partial result
    would keep the source "incomplete" (and its recommendations un-reconciled)
    forever. Connectors whose flag is read-only are left as they are.
    """
    try:
        setattr(connector, "last_collect_complete", True)
    except (AttributeError, TypeError):
        pass


def adapt_recommendation_source(connector: "RecommendationSource", deps: SourceDeps) -> RecommendationSourceAdapter:
    """Wrap a v1 connector instance; its ``name`` becomes the source id and the legacy source."""
    name = str(getattr(connector, "name", None) or type(connector).__name__).strip() or "unknown"
    doc = (type(connector).__doc__ or "").strip().splitlines()
    attrs: Dict[str, Any] = {
        "id": name,
        "display_name": name,
        # A v1 connector does not declare its domains: it may emit any capability.
        "capabilities": frozenset(Capability),
        "legacy_source": name,
        "description": doc[0] if doc else "",
        "__module__": __name__,
    }
    cls = type(f"{type(connector).__name__}Adapter", (RecommendationSourceAdapter,), attrs)
    return cls(SourceSettings(), deps, connector=connector)


__all__ = [
    "AnalyzerSource",
    "NATIVE_SOURCE_IDS",
    "RecommendationSourceAdapter",
    "adapt_recommendation_source",
    "analyzer_source_class",
    "native_source_id",
    "observed_window",
    "workload_inventory",
]
