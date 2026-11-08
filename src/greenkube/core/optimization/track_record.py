# src/greenkube/core/optimization/track_record.py
"""Source track record: Beta(alpha, beta) per (source family, capability) (roadmap I3).

Every measured outcome updates the record of the source family and capability
that produced the change: ``realized`` adds one to alpha, ``not_realized`` and
``rollback_review`` add one to beta. ``inconclusive`` and ``contaminated``
outcomes say nothing about the source and are only counted.

Outcomes come from the change-measurement repository when it offers
``get_outcome_counts_by_source_capability()``. Until then they are derived from
the verification state of applied recommendations by
:func:`outcome_counts_from_recommendations`, kept isolated so the source can be
rewired without touching the scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple, cast

logger = logging.getLogger(__name__)

#: Uniform prior of the exposed Beta distribution (one pseudo-success, one pseudo-failure).
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0

#: Outcome labels (roadmap C: measurement outcomes).
REALIZED = "realized"
NOT_REALIZED = "not_realized"
ROLLBACK = "rollback_review"
INCONCLUSIVE = "inconclusive"
CONTAMINATED = "contaminated"

#: Lifecycle statuses that can carry a measured outcome.
OUTCOME_STATUSES = ("applied", "verifying", "verified", "rollback_review", "regressed", "reverted")

#: Where the counts come from, reported by the API.
METHOD_MEASUREMENTS = "measurement_outcomes"
METHOD_RECOMMENDATION_STATUS = "recommendation_verification_status"

TrackKey = Tuple[str, str]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value.value if hasattr(value, "value") else value).strip().lower()


def source_family(source: Any) -> str:
    """Family a source belongs to; sources sharing an engine share a record.

    Goldilocks runs the VPA recommender, so its outcomes count for ``vpa``.
    """
    name = _text(source) or "greenkube"
    if name.startswith("push:"):
        return "push"
    return {"goldilocks": "vpa"}.get(name, name)


@dataclass(frozen=True)
class TrackRecord:
    """Measured outcomes of one source family on one capability."""

    source_family: str
    capability: str
    realized: int = 0
    not_realized: int = 0
    rollback: int = 0
    inconclusive: int = 0

    @property
    def failures(self) -> int:
        return self.not_realized + self.rollback

    @property
    def decided(self) -> int:
        """Outcomes that update the distribution."""
        return self.realized + self.failures

    @property
    def alpha(self) -> float:
        return PRIOR_ALPHA + self.realized

    @property
    def beta(self) -> float:
        return PRIOR_BETA + self.failures

    @property
    def mean(self) -> float:
        """Posterior mean of the realization rate under the uniform prior."""
        return self.alpha / (self.alpha + self.beta)

    def summary(self) -> str:
        """Human summary, e.g. ``vpa: 14/16 realized``."""
        return f"{self.source_family}: {self.realized}/{self.decided} realized"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_family": self.source_family,
            "capability": self.capability,
            "realized": self.realized,
            "not_realized": self.not_realized,
            "rollback_review": self.rollback,
            "inconclusive": self.inconclusive,
            "decided": self.decided,
            "alpha": self.alpha,
            "beta": self.beta,
            "mean": round(self.mean, 4),
            "summary": self.summary(),
        }


@dataclass(frozen=True)
class TrackRecordBook:
    """All track records, looked up by (source family, capability)."""

    records: Mapping[TrackKey, TrackRecord] = field(default_factory=dict)
    method: str = METHOD_RECOMMENDATION_STATUS

    def get(self, source: Any, capability: Any) -> TrackRecord:
        family = source_family(source)
        cap = _text(capability)
        return self.records.get((family, cap)) or TrackRecord(source_family=family, capability=cap)

    def entries(self) -> list[TrackRecord]:
        return [self.records[key] for key in sorted(self.records)]


EMPTY_BOOK = TrackRecordBook()


def outcome_of(record: Any) -> Optional[str]:
    """Measured outcome carried by a recommendation record, or None when there is none yet."""
    status = _text(getattr(record, "status", None))
    verification = _text(getattr(record, "verification_status", None))
    if status in ("rollback_review", "regressed", "reverted"):
        return ROLLBACK
    if status == "verified" or verification == "passed":
        return REALIZED
    if verification == "failed":
        return NOT_REALIZED
    if verification == INCONCLUSIVE:
        return INCONCLUSIVE
    return None


def outcome_counts_from_recommendations(records: Iterable[Any]) -> list[Dict[str, Any]]:
    """Derive outcome counts from recommendation verification state (interim I3 source).

    Isolated on purpose: once change measurement exposes
    ``get_outcome_counts_by_source_capability()`` the book is built from it instead.
    """
    counts: Dict[TrackKey, Dict[str, int]] = {}
    for record in records:
        outcome = outcome_of(record)
        if outcome is None:
            continue
        key = (source_family(getattr(record, "source", None)), _text(getattr(record, "capability", None)))
        bucket = counts.setdefault(key, {REALIZED: 0, NOT_REALIZED: 0, ROLLBACK: 0, INCONCLUSIVE: 0})
        bucket[outcome] += 1
    return [
        {"source_family": family, "capability": capability, **bucket}
        for (family, capability), bucket in sorted(counts.items())
    ]


def _count(row: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return 0


def book_from_counts(rows: Any, method: str = METHOD_MEASUREMENTS) -> TrackRecordBook:
    """Build a book from outcome-count rows.

    Accepts a list of mappings (``source_family``/``source``, ``capability`` and
    one count per outcome) or a mapping keyed by ``(source, capability)``.
    """
    items: list[Mapping[str, Any]] = []
    if isinstance(rows, Mapping):
        for key, value in rows.items():
            if isinstance(key, tuple) and len(key) == 2 and isinstance(value, Mapping):
                items.append({"source_family": key[0], "capability": key[1], **value})
    elif rows is not None:
        items = [row for row in rows if isinstance(row, Mapping)]

    records: Dict[TrackKey, TrackRecord] = {}
    for row in items:
        family = source_family(row.get("source_family") or row.get("source"))
        capability = _text(row.get("capability"))
        if not capability:
            continue
        previous = records.get((family, capability)) or TrackRecord(source_family=family, capability=capability)
        records[(family, capability)] = TrackRecord(
            source_family=family,
            capability=capability,
            realized=previous.realized + _count(row, REALIZED),
            not_realized=previous.not_realized + _count(row, NOT_REALIZED),
            rollback=previous.rollback + _count(row, ROLLBACK, "rollback"),
            inconclusive=previous.inconclusive + _count(row, INCONCLUSIVE) + _count(row, CONTAMINATED),
        )
    return TrackRecordBook(records=records, method=method)


async def load_track_record(reco_repo: Any) -> TrackRecordBook:
    """Load the track record; an unreadable store yields the empty book (priors only)."""
    getter = getattr(reco_repo, "get_outcome_counts_by_source_capability", None)
    try:
        if callable(getter):
            return book_from_counts(await cast(Any, getter)(), method=METHOD_MEASUREMENTS)
        records = await reco_repo.get_recommendations_by_statuses(list(OUTCOME_STATUSES))
        return book_from_counts(outcome_counts_from_recommendations(records), method=METHOD_RECOMMENDATION_STATUS)
    except Exception as exc:  # the ranking must never fail because the history is unreadable
        logger.warning("Could not load the source track record; using priors only: %s", exc)
        return EMPTY_BOOK
