"""Revision decisions shared by the recommendation repositories (roadmap E7).

A persisted recommendation keeps one identity across scans. Each scan's
consolidated proposal is compared with the stored one through
:func:`~greenkube.models.recommendation_v2.advice_signature` (what the
recommendation advises, without observed state, projections or wording):

* same advice: the record is refreshed in place, its revision is unchanged;
* new advice, no open plan: the previous proposal is kept as a ``superseded``
  revision and the record moves to ``revision + 1``;
* new advice under an open plan (``plan_ready``, ``pr_open``, ``merged``,
  ``sync_pending``, or the ``pr_open`` status): the record is not touched, the
  advice is ``queued`` as ``revision + 1`` and the plan's pull request shows
  "newer advice available". Once the plan is closed the next scan promotes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Tuple

from greenkube.models.metrics import RecommendationRecord
from greenkube.models.recommendation_v2 import (
    RecommendationRevision,
    advice_signature,
    as_recommendation,
    parse_proposal,
    revision_snapshot,
    v2_fields,
)
from greenkube.signals.mapping import proposal_for_recommendation
from greenkube.storage.recommendation_mapper import _as_datetime, from_json, row_to_record, to_json

logger = logging.getLogger(__name__)

#: Action statuses of a recommendation whose plan is open: its record is frozen.
OPEN_PLAN_ACTION_STATUSES = frozenset({"plan_ready", "pr_open", "merged", "sync_pending"})

SUPERSEDED = "superseded"
QUEUED = "queued"


@dataclass(frozen=True)
class RevisionDecision:
    #: ``refresh`` (same advice), ``revise`` (new revision on the record) or ``queue`` (frozen record).
    action: Literal["refresh", "revise", "queue"]
    #: Revision written on the record (refresh, revise) or of the queued advice (queue).
    revision: int


def is_frozen(status: Any, action_status: Any) -> bool:
    """True when a plan is open on the record (it must not change, E7)."""
    status_text = str(getattr(status, "value", status) or "")
    action_text = str(getattr(action_status, "value", action_status) or "")
    return status_text == "pr_open" or action_text in OPEN_PLAN_ACTION_STATUSES


def decide(existing: RecommendationRecord, incoming: RecommendationRecord, *, frozen: bool) -> RevisionDecision:
    """How an incoming proposal is written against the stored record."""
    current = max(1, int(existing.revision or 1))
    if advice_signature(existing) == advice_signature(incoming):
        return RevisionDecision("refresh", current)
    if frozen:
        return RevisionDecision("queue", current + 1)
    return RevisionDecision("revise", current + 1)


def proposal_of(record: RecommendationRecord) -> Optional[Dict[str, Any]]:
    """The stored typed proposal, or the one derived from the flat columns (rows written before v2)."""
    proposal = parse_proposal(record.proposal)
    if proposal is None:
        try:
            proposal = proposal_for_recommendation(as_recommendation(record))
        except ValueError:
            return None
    return proposal.model_dump(mode="json")


def revision_values(record: RecommendationRecord) -> Dict[str, Any]:
    """``proposal``, ``consensus`` and ``snapshot`` of one revision of ``record``."""
    return {
        "proposal": proposal_of(record),
        "consensus": record.consensus,
        "snapshot": revision_snapshot(record),
    }


def revision_from_row(row: Any) -> RecommendationRevision:
    """A ``recommendation_revisions`` row (SQLite or PostgreSQL)."""
    return RecommendationRevision(
        recommendation_id=int(row["recommendation_id"]),
        revision=int(row["revision"]),
        state=str(row["state"]),
        proposal=_dict(row["proposal"]),
        consensus=_dict(row["consensus"]),
        snapshot=_dict(row["snapshot"]) or {},
        created_at=_as_datetime(row["created_at"]),
        updated_at=_as_datetime(row["updated_at"]),
    )


def backfill_update(row: Any, cluster_uid: str) -> Optional[Tuple[Optional[str], Optional[str], str, int]]:
    """``(target, proposal, fingerprint_v4, id)`` of a row written before v2, or None when underivable."""
    try:
        fields = v2_fields(row_to_record(row), cluster_uid)
    except ValueError as exc:
        logger.debug("Cannot derive the v2 view of recommendation %s (%s).", row["id"], type(exc).__name__)
        return None
    return (to_json(fields["target"]), to_json(fields["proposal"]), fields["fingerprint_v4"], int(row["id"]))


def _dict(value: Any) -> Optional[Dict[str, Any]]:
    parsed = from_json(value)
    return parsed if isinstance(parsed, dict) else None
