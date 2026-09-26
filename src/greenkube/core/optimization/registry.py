# src/greenkube/core/optimization/registry.py
"""Builds the recommendation source list from configuration."""

import logging
from typing import TYPE_CHECKING, Dict, List

from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.providers.karpenter import KarpenterSource
from greenkube.core.optimization.providers.native import NativeSource
from greenkube.core.optimization.providers.vpa import VpaSource

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
    """Instantiates the enabled recommendation sources with their priorities."""
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
