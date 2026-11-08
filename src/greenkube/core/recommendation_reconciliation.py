# src/greenkube/core/recommendation_reconciliation.py
"""Per-source reconciliation rules and run diffs (E8, E10).

A recommendation becomes ``stale`` only when every source that supported it
finished this run with an authoritative (complete) result and none of them
re-emitted it. A source that failed, timed out, was unavailable or reported
an incomplete result never causes staleness: its previous recommendations are
kept as "last known" until it completes again or their TTL expires.

Sources that are no longer configured at all are not "incomplete" (they will
never report again), so their recommendations reconcile normally.
"""

from dataclasses import dataclass, field
from typing import Any, Collection, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from greenkube.models.metrics import RecommendationRecord
from greenkube.models.optimization_run import DIFF_SAMPLE_LIMIT, OptimizationRunDiff


def _value(item: Any) -> Optional[str]:
    if item is None:
        return None
    value = item.value if hasattr(item, "value") else item
    text = str(value).strip()
    return text or None


def supporting_sources(source: Any, sources: Optional[Iterable[Any]]) -> FrozenSet[str]:
    """Return the sources that support a persisted recommendation.

    ``sources`` is the provenance list written at generation time; the primary
    ``source`` is always included so legacy rows without provenance still map
    to their connector.
    """
    supported = {value for value in (_value(item) for item in (sources or [])) if value}
    primary = _value(source)
    if primary:
        supported.add(primary)
    return frozenset(supported)


def may_mark_stale(source: Any, sources: Optional[Iterable[Any]], incomplete_sources: Collection[str]) -> bool:
    """True when no supporting source of a missing recommendation was incomplete in this run."""
    if not incomplete_sources:
        return True
    return supporting_sources(source, sources).isdisjoint(incomplete_sources)


def proposal_signature(record: RecommendationRecord) -> Tuple:
    """The part of a recommendation whose change means the advice changed.

    Projected savings drift on every scan and are deliberately excluded.
    """
    return (
        _value(record.source),
        record.current_cpu_request_millicores,
        record.recommended_cpu_request_millicores,
        record.current_memory_request_bytes,
        record.recommended_memory_request_bytes,
        record.cron_schedule,
        record.target_node,
    )


@dataclass
class RecommendationDiff:
    """Full diff between the active set before a run and the run's persisted output."""

    new: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    resolved: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    retained: List[str] = field(default_factory=list)

    def to_run_diff(self, limit: int = DIFF_SAMPLE_LIMIT) -> OptimizationRunDiff:
        truncated = any(len(items) > limit for items in (self.new, self.changed, self.resolved))
        return OptimizationRunDiff(
            new=self.new[:limit],
            changed=self.changed[:limit],
            resolved=self.resolved[:limit],
            truncated=truncated,
        )


def compute_diff(
    previous: Sequence[RecommendationRecord],
    current: Sequence[RecommendationRecord],
    incomplete_sources: Collection[str] = (),
) -> RecommendationDiff:
    """Classify identities as new, changed, unchanged, resolved or retained.

    ``retained`` are previously active recommendations that were not re-emitted
    but are kept because a supporting source did not complete; they are not
    resolved.
    """
    previous_by_fp: Dict[str, RecommendationRecord] = {}
    for record in previous:
        if record.fingerprint:
            previous_by_fp.setdefault(record.fingerprint, record)
    current_by_fp: Dict[str, RecommendationRecord] = {}
    for record in current:
        if record.fingerprint:
            current_by_fp.setdefault(record.fingerprint, record)

    diff = RecommendationDiff()
    for fingerprint, record in current_by_fp.items():
        before = previous_by_fp.get(fingerprint)
        if before is None:
            diff.new.append(fingerprint)
        elif proposal_signature(before) != proposal_signature(record):
            diff.changed.append(fingerprint)
        else:
            diff.unchanged.append(fingerprint)
    for fingerprint, record in previous_by_fp.items():
        if fingerprint in current_by_fp:
            continue
        if may_mark_stale(record.source, record.sources, incomplete_sources):
            diff.resolved.append(fingerprint)
        else:
            diff.retained.append(fingerprint)
    for items in (diff.new, diff.changed, diff.resolved, diff.unchanged, diff.retained):
        items.sort()
    return diff
