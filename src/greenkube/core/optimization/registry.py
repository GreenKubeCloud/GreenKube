"""Recommendation sources: built-in registrations and configuration (roadmap E5).

The engine runs Source SDK sources (:mod:`greenkube.signals`):

* one source per native analyzer (``greenkube.rightsizing_cpu``, ``greenkube.orphaned_lb``...),
  built by the analyzer adapter;
* ``vpa``, the VPA connector;
* ``greenkube.nodepool``, the NodePool consolidation heuristic over Karpenter objects;
* third-party sources registered under the ``greenkube.sources`` entry points.

:func:`build_sources` (v1 ``RecommendationSource`` connectors) is kept for
callers that still use the v1 contract.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.providers.karpenter import KarpenterSource, NodePoolSignalSource
from greenkube.core.optimization.providers.native import NativeSource
from greenkube.core.optimization.providers.vpa import VpaSignalSource, VpaSource
from greenkube.signals.registry import ConfiguredSource, SourceRegistry, instantiate, parse_sources_config
from greenkube.signals.sdk import EnabledMode, Source, SourceDeps

if TYPE_CHECKING:
    from greenkube.core.config import Config

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_PRIORITY = "vpa,karpenter,greenkube"


def parse_source_priority(value: str | None) -> Dict[str, int]:
    """Parses the priority string into a ``{source: weight}`` map.

    The first source in the list has the highest weight so that arbitration
    picks it when multiple sources own the same capability.
    """
    order = [item.strip().lower() for item in (value or "").split(",") if item.strip()]
    if not order:
        order = DEFAULT_SOURCE_PRIORITY.split(",")
    size = len(order)
    return {name: size - index for index, name in enumerate(order)}


def build_sources(config: "Config") -> List[RecommendationSource]:
    """Instantiates the enabled v1 recommendation connectors with their priorities."""
    sources: List[RecommendationSource] = []

    if getattr(config, "RECOMMENDATION_VPA_ENABLED", False):
        sources.append(VpaSource(config))
    if getattr(config, "RECOMMENDATION_KARPENTER_ENABLED", False):
        sources.append(KarpenterSource(config))

    # Native analysis is always enabled.
    sources.append(NativeSource(config))

    priorities = parse_source_priority(getattr(config, "RECOMMENDATION_SOURCE_PRIORITY", DEFAULT_SOURCE_PRIORITY))
    for source in sources:
        source.priority = priorities.get(source.name, 0)

    logger.debug(
        "Optimization sources: %s",
        ", ".join(f"{s.name}(p={s.priority})" for s in sources),
    )
    return sources


def builtin_source_classes() -> List[type[Source]]:
    """Built-in sources, in execution order (VPA, NodePool, the native analyzers, then the signal-only connectors)."""
    from greenkube.core.optimization.analyzers import DEFAULT_ANALYZERS
    from greenkube.signals.adapters import analyzer_source_class
    from greenkube.signals.connectors.gke import GkeRecommenderSource
    from greenkube.signals.connectors.krr import KrrSource
    from greenkube.signals.connectors.kubecost import KubecostSource
    from greenkube.signals.connectors.polaris import PolarisSource
    from greenkube.signals.connectors.replay import PushSignalSource

    return [
        VpaSignalSource,
        NodePoolSignalSource,
        *(analyzer_source_class(cls) for cls in DEFAULT_ANALYZERS),
        KrrSource,
        KubecostSource,
        PushSignalSource,
        PolarisSource,
        GkeRecommenderSource,
    ]


def legacy_enablement(config: "Config") -> Dict[str, EnabledMode]:
    """Enablement from the v1 flags; ``SOURCES_CONFIG`` overrides them.

    The v1 flags already meant "enabled when the CRD is detected", i.e. ``auto``.
    """
    flags = (
        (VpaSignalSource.id, "RECOMMENDATION_VPA_ENABLED"),
        (NodePoolSignalSource.id, "RECOMMENDATION_KARPENTER_ENABLED"),
    )
    return {
        source_id: EnabledMode.AUTO if getattr(config, flag, True) else EnabledMode.FALSE for source_id, flag in flags
    }


def cluster_uid(config: "Config") -> str:
    """Cluster identity used in signal targets."""
    return str(getattr(config, "CLUSTER_NAME", "") or "default")


def build_source_registry(
    config: "Config",
    *,
    entry_points: Optional[Callable[[], Iterable[Any]]] = None,
) -> Tuple[SourceRegistry, List[ConfiguredSource]]:
    """The registry (built-ins, then plugins when enabled) and every source's resolved configuration."""
    registry = SourceRegistry()
    for source_class in builtin_source_classes():
        registry.register(source_class, origin="builtin")
    if getattr(config, "SOURCES_PLUGINS_ENABLED", True):
        registry.load_plugins(entry_points)
    try:
        settings = parse_sources_config(getattr(config, "SOURCES_CONFIG", ""))
    except ValueError as exc:
        # Config validation rejects this at startup; stay defensive for mutated configs.
        logger.error("Ignoring invalid SOURCES_CONFIG: %s", exc)
        settings = {}
    return registry, registry.configure(settings, legacy_enablement(config))


def build_signal_sources(
    config: "Config",
    *,
    entry_points: Optional[Callable[[], Iterable[Any]]] = None,
) -> List[Source]:
    """Instantiate the enabled sources for one scan."""
    _registry, configured = build_source_registry(config, entry_points=entry_points)
    for item in configured:
        if item.error is not None:
            logger.error("Source '%s' is not configured correctly: %s", item.id, item.error)
    sources, errors = instantiate(configured, SourceDeps(config=config, cluster_uid=cluster_uid(config)))
    logger.debug("Recommendation sources: %s", ", ".join(source.source_id for source in sources))
    for source_id, error in errors.items():
        logger.error("Source '%s' could not start: %s", source_id, error)
    return sources
