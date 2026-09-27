# src/greenkube/core/optimization/dedup.py
"""Deduplication, provenance and cross-source arbitration.

When several sources observe the same issue on the same target, the highest
priority source wins and the others are dropped. This is what prevents a native
rightsizing recommendation from being emitted alongside a VPA recommendation
for the same workload.
"""

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Dict, List, Tuple

from greenkube.core.optimization.thresholds import apply_minimum_thresholds
from greenkube.models.metrics import Recommendation, RecommendationCapability, RecommendationSource, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.config import Config

logger = logging.getLogger(__name__)


def _source_value(rec: Recommendation) -> str:
    source = rec.source
    return source.value if isinstance(source, RecommendationSource) else str(source)


def _capability_value(rec: Recommendation) -> str:
    capability = rec.capability
    return capability.value if isinstance(capability, RecommendationCapability) else str(capability)


def _type_value(rec: Recommendation) -> str:
    rec_type = rec.type
    return rec_type.value if isinstance(rec_type, RecommendationType) else str(rec_type)


def _source_priority_map(config: "Config") -> Dict[str, int]:
    from greenkube.core.optimization.registry import parse_source_priority

    return parse_source_priority(getattr(config, "RECOMMENDATION_SOURCE_PRIORITY", None))


def arbitration_key(rec: Recommendation) -> Tuple:
    """Returns the key used to detect cross-source conflicts."""
    if rec.owner_kind and rec.owner_name:
        target: Tuple = (rec.namespace, rec.owner_kind, rec.owner_name)
    elif rec.target_node:
        target = (None, "Node", rec.target_node)
    elif rec.pod_name:
        target = (rec.namespace, "Pod", rec.pod_name)
    else:
        target = (rec.namespace, "Namespace", None)
    return (*target, _capability_value(rec), _type_value(rec))


def _normalize_provenance(rec: Recommendation) -> Recommendation:
    """Ensures ``sources`` contains at least the recommendation's own source."""
    source_value = _source_value(rec)
    sources = list(rec.sources) if rec.sources else []
    if source_value not in sources:
        sources.insert(0, source_value)
    if sources != rec.sources:
        return rec.model_copy(update={"sources": sources})
    return rec


def arbitrate(recs: List[Recommendation], priorities: Dict[str, int]) -> List[Recommendation]:
    """Keeps one recommendation per (target, capability, type), highest priority wins."""
    groups: "OrderedDict[Tuple, List[Recommendation]]" = OrderedDict()
    for rec in recs:
        groups.setdefault(arbitration_key(rec), []).append(rec)

    result: List[Recommendation] = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        ordered = sorted(group, key=lambda r: priorities.get(_source_value(r), 0), reverse=True)
        winner = ordered[0]
        merged_sources: List[str] = []
        for rec in ordered:
            value = _source_value(rec)
            if value not in merged_sources:
                merged_sources.append(value)

        if len(group) > 1:
            logger.debug(
                "Arbitration kept source '%s' over %s for %s",
                _source_value(winner),
                [s for s in merged_sources if s != _source_value(winner)],
                arbitration_key(winner),
            )
        result.append(winner.model_copy(update={"sources": merged_sources}))

    return result


def deduplicate(recs: List[Recommendation]) -> List[Recommendation]:
    """Removes duplicate recommendations with the same target and type."""
    seen = set()
    result = []
    for rec in recs:
        key = (rec.scope, rec.namespace, rec.pod_name, rec.type, rec.target_node or "")
        if key not in seen:
            seen.add(key)
            result.append(rec)
    return result


def finalize_recommendations(recs: List[Recommendation], config: "Config") -> List[Recommendation]:
    """Normalizes provenance, arbitrates conflicts, deduplicates and clamps values."""
    priorities = _source_priority_map(config)
    normalized = [_normalize_provenance(rec) for rec in recs]
    arbitrated = arbitrate(normalized, priorities)
    deduped = deduplicate(arbitrated)
    return [apply_minimum_thresholds(rec, config) for rec in deduped]
