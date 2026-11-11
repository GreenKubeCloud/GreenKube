"""Recommendation v2 (roadmap E4, E7): target, typed proposal, consensus and revisions.

A persisted recommendation (``recommendation_history``) keeps its v1 flat
columns and gains the v2 view:

* ``target``: the canonical, cluster-qualified :class:`TargetRef`;
* ``proposal``: the consolidated typed proposal; the v1 flat fields
  (``recommended_cpu_request_millicores``, ``cron_schedule``...) are the same
  advice in flat form, so v1 → v2 → v1 is lossless;
* ``consensus``: how the proposal was chosen across sources (E6);
* ``fingerprint_v4 = sha256(cluster_uid, target.key(), capability, kind)``;
* ``revision``: increases each time the consolidated proposal changes (E7).

``kind`` is part of the v4 identity because GreenKube still persists one record
per resource (``RIGHTSIZING_CPU`` and ``RIGHTSIZING_MEMORY`` share the
``container_requests`` capability); when one record carries both resources
(roadmap H1) the kind becomes the capability's single kind and the identity
reduces to ``(cluster_uid, target, capability)``.

Rows written before v2 have NULL v2 columns: :func:`v2_fields` derives them
from the flat columns, for the read path and for the backfill.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.signals.consensus import Consensus, ConsensusVote
from greenkube.signals.mapping import (
    SOURCE_FIELDS,
    capability_for,
    proposal_for_recommendation,
    recommendation_from_signal,
    target_for_recommendation,
)
from greenkube.signals.model import AlgorithmInfo, Capability, Proposal, SignalEvidence, SourceSignal, TargetRef

#: Version of the identity algorithm behind ``fingerprint_v4``.
IDENTITY_V4 = 4

_PROPOSAL = TypeAdapter(Proposal)

#: v1 fields the flat view of a v2 recommendation is compared on (round trip).
FLAT_FIELDS = tuple(
    name
    for name in SOURCE_FIELDS
    if name not in ("potential_savings_cost", "potential_savings_co2e_grams", "impact", "sources")
)
#: v1 proposal fields kept in a revision snapshot.
SNAPSHOT_FIELDS = (
    "type",
    "description",
    "reason",
    "priority",
    "source",
    "sources",
    "current_cpu_request_millicores",
    "recommended_cpu_request_millicores",
    "current_memory_request_bytes",
    "recommended_memory_request_bytes",
    "cron_schedule",
    "potential_savings_cost",
    "potential_savings_co2e_grams",
    "impact",
)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def compute_fingerprint_v4(target: TargetRef, capability: Any, kind: Any) -> str:
    """``sha256(cluster_uid, target.key(), capability, kind)``: stable across scans, sources and revisions."""
    payload = json.dumps(
        [target.cluster_uid, target.key(), _text(capability), _text(kind)],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def as_recommendation(record: Union[Recommendation, RecommendationRecord]) -> Recommendation:
    """The v1 recommendation fields of a record (mapping helpers take a ``Recommendation``)."""
    if isinstance(record, Recommendation):
        return record
    return Recommendation.model_validate(record.model_dump(include=set(SOURCE_FIELDS) | {"evidence", "confidence"}))


def parse_target(value: Any) -> Optional[TargetRef]:
    if isinstance(value, TargetRef):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return TargetRef.model_validate(value)
    except ValueError:
        return None


def parse_proposal(value: Any) -> Any:
    if value is None:
        return None
    try:
        return _PROPOSAL.validate_python(value)
    except ValueError:
        return None


def v2_fields(record: Union[Recommendation, RecommendationRecord], cluster_uid: str) -> Dict[str, Any]:
    """``target``, ``proposal`` and ``fingerprint_v4`` of a record: stored values first, else derived."""
    rec = as_recommendation(record)
    target = parse_target(record.target) or target_for_recommendation(rec, cluster_uid)
    proposal = parse_proposal(record.proposal)
    if proposal is None:
        proposal = proposal_for_recommendation(rec)
    return {
        "target": target.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"),
        "fingerprint_v4": compute_fingerprint_v4(target, capability_for(rec.type), _text(rec.type)),
    }


def with_v2_fields(record: Any, cluster_uid: str) -> Any:
    """``record`` (Recommendation or RecommendationRecord) with its v2 fields filled when missing."""
    if record.fingerprint_v4 and record.target is not None and record.proposal is not None:
        return record
    fields = v2_fields(record, cluster_uid)
    update = {name: value for name, value in fields.items() if getattr(record, name) in (None, "")}
    return record.model_copy(update=update) if update else record


def advice_signature(record: Any) -> tuple:
    """What a recommendation advises; a change is a new revision (E7).

    Observed state (``current_*``), projected savings and explanations drift
    between scans without changing the advice and are excluded. A typed
    proposal that the flat fields cannot express (HPA bounds, a NodePool
    patch) is compared through its identity.
    """
    proposal = parse_proposal(getattr(record, "proposal", None))
    extra: Any = None
    if proposal is not None and getattr(proposal, "kind", None) in ("hpa", "node_pool", "replicas"):
        from greenkube.signals.model import proposal_identity

        extra = json.dumps(proposal_identity(proposal), sort_keys=True, default=str)
    return (
        _text(getattr(record, "type", None)),
        getattr(record, "recommended_cpu_request_millicores", None),
        getattr(record, "recommended_memory_request_bytes", None),
        getattr(record, "cron_schedule", None),
        extra,
    )


def revision_snapshot(record: RecommendationRecord) -> Dict[str, Any]:
    """The v1 view of one revision, kept in ``recommendation_revisions``."""
    return record.model_dump(mode="json", include=set(SNAPSHOT_FIELDS))


class RecommendationRisk(BaseModel):
    level: Optional[str] = None
    factors: List[str] = Field(default_factory=list)
    effort: Optional[str] = None
    reversible: Optional[bool] = None
    requires_restart: Optional[bool] = None


class RecommendationLifecycle(BaseModel):
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    application_method: Optional[str] = None
    verification_status: Optional[str] = None
    verified_at: Optional[datetime] = None
    ignored_at: Optional[datetime] = None
    ignored_reason: Optional[str] = None


class RecommendationRevision(BaseModel):
    """One revision of a recommendation's consolidated proposal."""

    model_config = ConfigDict(extra="ignore")

    recommendation_id: int
    revision: int
    #: ``current`` (the record), ``superseded`` (replaced by a later revision) or
    #: ``queued`` (computed while a plan was open: shown as "newer advice available").
    state: str
    proposal: Optional[Dict[str, Any]] = None
    consensus: Optional[Dict[str, Any]] = None
    snapshot: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NewerRevision(BaseModel):
    """A recommendation of a plan whose advice changed after the plan pinned it (E7, J5)."""

    plan_id: str
    recommendation_id: int
    pinned_revision: int
    latest_revision: int
    #: True when the newer revision is queued behind the open plan (the record is frozen).
    queued: bool = False
    snapshot: Dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        """``recommendation #12 is at revision 3 (this change implements revision 2): CPU request 250m.``"""
        text = (
            f"recommendation #{self.recommendation_id} is at revision {self.latest_revision} "
            f"(this change implements revision {self.pinned_revision})"
        )
        advice = _advice_text(self.snapshot)
        return f"{text}: {advice}." if advice else f"{text}."


def _advice_text(snapshot: Dict[str, Any]) -> str:
    cpu = snapshot.get("recommended_cpu_request_millicores")
    memory = snapshot.get("recommended_memory_request_bytes")
    parts = []
    if isinstance(cpu, int):
        parts.append(f"CPU request {cpu}m")
    if isinstance(memory, int):
        parts.append(f"memory request {memory / (1024 * 1024):.0f}Mi")
    if not parts and isinstance(snapshot.get("cron_schedule"), str):
        parts.append(snapshot["cron_schedule"])
    return ", ".join(parts)


class RecommendationV2(BaseModel):
    """API v2 shape of a recommendation (``/api/v2/recommendations``)."""

    id: Optional[int] = None
    #: v4 identity (``fingerprint_v4``).
    fingerprint: str
    #: v3 identity of the v1 API.
    legacy_fingerprint: Optional[str] = None
    revision: int = 1
    #: Revision computed while a plan was open, waiting behind it ("newer advice available").
    queued_revision: Optional[int] = None
    target: TargetRef
    capability: Capability
    #: Finding kind (the v1 type, ``RIGHTSIZING_CPU``...).
    kind: str
    proposal: Proposal
    #: Signals that support the recommendation, from the consensus.
    signals: List[ConsensusVote] = Field(default_factory=list)
    consensus: Optional[Consensus] = None
    impact: Optional[Dict[str, Any]] = None
    value: Optional[Dict[str, Any]] = None
    risk: RecommendationRisk = Field(default_factory=RecommendationRisk)
    confidence: Optional[float] = None
    eligibility: Optional[Dict[str, Any]] = None
    status: str = "active"
    lifecycle: RecommendationLifecycle = Field(default_factory=RecommendationLifecycle)
    source: str = "greenkube"
    sources: List[str] = Field(default_factory=list)
    description: str = ""
    reason: str = ""
    priority: str = "medium"
    evidence: Optional[RecommendationEvidence] = None
    #: v1 flat fields that the target and the proposal cannot express (scope, legacy naming).
    legacy_fields: Dict[str, Any] = Field(default_factory=dict)
    #: Revision history, newest first (detail endpoint only).
    revisions: Optional[List[RecommendationRevision]] = None


def _signal_for(target: TargetRef, kind: str, proposal: Any, description: str = "", reason: str = "") -> SourceSignal:
    return SourceSignal(
        source_id="greenkube.consensus",
        target=target,
        capability=capability_for(kind),
        kind=kind,
        proposal=proposal,
        algorithm=AlgorithmInfo(name="greenkube.consensus"),
        evidence=SignalEvidence(summary=description, details=reason),
    )


def derived_flat_fields(target: TargetRef, kind: str, proposal: Any) -> Dict[str, Any]:
    """v1 flat fields computed from the target and the proposal alone."""
    rec = recommendation_from_signal(_signal_for(target, kind, proposal))
    return rec.model_dump(mode="json", include=set(FLAT_FIELDS) - {"description", "reason", "source"})


def to_v2(
    record: RecommendationRecord,
    cluster_uid: str,
    *,
    queued_revision: Optional[int] = None,
    revisions: Optional[List[RecommendationRevision]] = None,
) -> RecommendationV2:
    """The v2 view of a persisted recommendation (stored v2 columns first, else derived from the flat ones)."""
    fields = v2_fields(record, cluster_uid)
    target = TargetRef.model_validate(fields["target"])
    proposal = _PROPOSAL.validate_python(fields["proposal"])
    kind = _text(record.type)
    consensus = None
    if isinstance(record.consensus, dict):
        try:
            consensus = Consensus.model_validate(record.consensus)
        except ValueError:
            consensus = None
    actual = record.model_dump(mode="json", include=set(FLAT_FIELDS))
    derived = derived_flat_fields(target, kind, proposal)
    legacy = {
        name: value
        for name, value in actual.items()
        if name in derived and derived.get(name) != value and name not in ("type", "capability")
    }
    return RecommendationV2(
        id=record.id,
        fingerprint=record.fingerprint_v4 or fields["fingerprint_v4"],
        legacy_fingerprint=record.fingerprint,
        revision=record.revision,
        queued_revision=queued_revision,
        target=target,
        capability=capability_for(kind),
        kind=kind,
        proposal=proposal,
        signals=list(consensus.votes) if consensus is not None else [],
        consensus=consensus,
        impact=record.impact,
        value=record.value,
        risk=RecommendationRisk(
            level=_text(record.risk_level) or None,
            factors=list(record.risk_factors),
            effort=_text(record.effort) or None,
            reversible=record.reversible,
            requires_restart=record.requires_restart,
        ),
        confidence=record.confidence,
        eligibility=record.eligibility,
        status=_text(record.status) or "active",
        lifecycle=RecommendationLifecycle(
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            applied_at=record.applied_at,
            application_method=record.application_method,
            verification_status=record.verification_status,
            verified_at=record.verified_at,
            ignored_at=record.ignored_at,
            ignored_reason=record.ignored_reason,
        ),
        source=_text(record.source) or "greenkube",
        sources=list(record.sources),
        description=record.description,
        reason=record.reason,
        priority=record.priority,
        evidence=record.evidence,
        legacy_fields=legacy,
        revisions=revisions,
    )


def flat_fields(v2: RecommendationV2) -> Dict[str, Any]:
    """The v1 flat fields of a v2 recommendation: computed from the proposal, then its legacy fields."""
    fields = derived_flat_fields(v2.target, v2.kind, v2.proposal)
    fields.update(
        {
            "type": RecommendationType(v2.kind).value,
            "description": v2.description,
            "reason": v2.reason,
            "priority": v2.priority,
            "source": v2.source,
        }
    )
    fields.update(v2.legacy_fields)
    return fields
