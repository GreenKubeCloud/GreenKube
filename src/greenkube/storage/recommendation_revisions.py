"""Revision decisions shared by the recommendation repositories (roadmap E7).

A persisted recommendation keeps one identity across scans. Each scan's
consolidated proposal is compared with the stored one through
:func:`~greenkube.models.recommendation_v2.advice_signature` (what the
recommendation advises, without observed state, projections or wording):

* same advice, no open plan: the record is refreshed in place, its revision is unchanged;
* same advice under an open plan: the record is kept as the plan pinned it,
  only its liveness (``expires_at``) is refreshed. Its typed action is shared
  with the other recommendations of the workload bundle (a CPU and a memory
  change of one container): rewriting it from a scan whose sibling advice
  moved would make the members' copies diverge under the plan;
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

#: SQL condition (both backends) true when no plan is open on a ``recommendation_history`` row.
#: A revising UPDATE carries it so a plan opened between the read and the write still freezes the record.
NO_OPEN_PLAN_SQL = "status = 'active' AND COALESCE(action_status, '') NOT IN ({})".format(
    ", ".join(f"'{status}'" for status in sorted(OPEN_PLAN_ACTION_STATUSES))
)

SUPERSEDED = "superseded"
QUEUED = "queued"

#: Superseded revisions kept per recommendation (newest first). Older ones are pruned
#: unless a plan pins them, so a drifting proposal cannot grow the table without bound.
MAX_SUPERSEDED_REVISIONS = 20


def prune_superseded_sql(placeholder) -> str:
    """DELETE of the superseded revisions beyond :data:`MAX_SUPERSEDED_REVISIONS` (unpinned only).

    ``placeholder(n)`` renders the n-th bind parameter (``?`` on SQLite, ``$n``
    on PostgreSQL); every parameter is the recommendation id.
    """
    return (
        "DELETE FROM recommendation_revisions "
        f"WHERE recommendation_id = {placeholder(1)} AND state = '{SUPERSEDED}' "
        "AND revision NOT IN ("
        "SELECT revision FROM recommendation_revisions "
        f"WHERE recommendation_id = {placeholder(2)} AND state = '{SUPERSEDED}' "
        f"ORDER BY revision DESC LIMIT {MAX_SUPERSEDED_REVISIONS}) "
        "AND revision NOT IN ("
        f"SELECT revision FROM recommendation_plan_pins WHERE recommendation_id = {placeholder(3)})"
    )


@dataclass(frozen=True)
class RevisionDecision:
    #: ``refresh`` (same advice), ``keep`` (same advice, frozen record: liveness only),
    #: ``revise`` (new revision on the record) or ``queue`` (new advice, frozen record).
    action: Literal["refresh", "keep", "revise", "queue"]
    #: Revision written on the record (refresh, revise) or of the queued advice (queue).
    revision: int


def is_frozen(status: Any, action_status: Any) -> bool:
    """True when a plan is open on the record (it must not change, E7)."""
    status_text = str(getattr(status, "value", status) or "")
    action_text = str(getattr(action_status, "value", action_status) or "")
    return status_text == "pr_open" or action_text in OPEN_PLAN_ACTION_STATUSES


#: Proposal fields recorded when ignored advice changes (REC-06).
_IGNORED_CHANGE_FIELDS = (
    "current_cpu_request_millicores",
    "recommended_cpu_request_millicores",
    "current_memory_request_bytes",
    "recommended_memory_request_bytes",
    "potential_savings_cost",
    "potential_savings_co2e_grams",
)


def ignored_advice_change(ignored: RecommendationRecord, incoming: RecommendationRecord) -> Optional[Dict[str, Any]]:
    """What changed materially in advice the user ignored, or None (REC-06).

    A restatement, or a request change within the revision hysteresis in the
    same direction, is not a change. Anything else (a move beyond the band,
    the opposite direction such as an OOM-driven increase after an ignored
    decrease, other advice) returns the new proposal with the ignored one.
    """
    if advice_signature(ignored) == advice_signature(incoming):
        return None
    from greenkube.core.optimization.dedup import within_hysteresis

    if within_hysteresis(incoming, ignored):
        return None
    details: Dict[str, Any] = {field: getattr(incoming, field) for field in _IGNORED_CHANGE_FIELDS}
    details["ignored"] = {field: getattr(ignored, field) for field in _IGNORED_CHANGE_FIELDS}
    details["reason"] = incoming.reason
    return details


def same_ignored_change(details: Dict[str, Any], previous: Any) -> bool:
    """True when ``previous`` (a stored event's details) already records the proposal of ``details``."""
    if not isinstance(previous, dict):
        return False
    return all(previous.get(field) == details.get(field) for field in _IGNORED_CHANGE_FIELDS[:4])


def decide(existing: RecommendationRecord, incoming: RecommendationRecord, *, frozen: bool) -> RevisionDecision:
    """How an incoming proposal is written against the stored record."""
    current = max(1, int(existing.revision or 1))
    if advice_signature(existing) == advice_signature(incoming):
        return RevisionDecision("keep" if frozen else "refresh", current)
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
