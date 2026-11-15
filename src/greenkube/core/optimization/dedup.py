# src/greenkube/core/optimization/dedup.py
"""Consolidation of every source's output into one recommendation per target (roadmap E6).

The engine passes the signals of every source, each with the v1 recommendation
of built-in sources (native analyzers, VPA, the NodePool heuristic). Signals
are grouped by target, capability and finding kind and combined by the
:mod:`consensus engine <greenkube.signals.consensus>` (policy
``RECOMMENDATION_CONSENSUS_POLICY``); no source's proposal is discarded
unseen any more: every supporting source is listed in ``sources`` and in the
consensus spread.

The consolidated recommendation is built on the v1 recommendation of the
source whose proposal was chosen. When the chosen proposal comes from a
signal-only source (a third-party connector, a pushed signal), it is built on
a built-in recommendation of the same group with the chosen value, or from
the signal itself; its impact is projected again by the caller (the same
Impact Model as every other projection), never copied from another value.
Signal-only sources therefore become recommendations only through consensus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from greenkube.core.optimization.thresholds import apply_minimum_thresholds
from greenkube.models.metrics import (
    Recommendation,
    RecommendationRecord,
    RecommendationSource,
    RecommendationType,
    coerce_recommendation_source,
    source_text,
)
from greenkube.signals.consensus import (
    KIND_REQUEST_RESOURCES,
    Ballot,
    ConsensusOutcome,
    ConsensusPolicy,
    ballots_for_signal,
    consolidate,
    parse_policy,
)
from greenkube.signals.mapping import recommendation_from_signal, signal_from_recommendation
from greenkube.signals.model import Capability, SourceSignal

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.track_record import TrackRecordBook
    from greenkube.signals.runtime import SourceReport

logger = logging.getLogger(__name__)

#: v1 recommendation field of each consolidated request resource.
RESOURCE_FIELDS: Dict[str, Tuple[str, str]] = {
    "cpu_request_m": ("current_cpu_request_millicores", "recommended_cpu_request_millicores"),
    "memory_request_bytes": ("current_memory_request_bytes", "recommended_memory_request_bytes"),
}

#: Adjusts a consolidated recommendation whose value no built-in recommendation projected:
#: fills the observed current request and projects its impact. Returns it unchanged when it cannot.
Refine = Callable[[Recommendation], Recommendation]


def _source_value(rec: Recommendation) -> str:
    return source_text(rec.source)


def _type_value(rec: Recommendation) -> str:
    return source_text(rec.type)


def _source_priority_map(config: "Config") -> Dict[str, int]:
    from greenkube.core.optimization.registry import parse_source_priority

    return parse_source_priority(getattr(config, "RECOMMENDATION_SOURCE_PRIORITY", None))


def consensus_policy(config: "Config") -> ConsensusPolicy:
    """The configured consensus policy (``safest`` when unset or invalid)."""
    try:
        return parse_policy(getattr(config, "RECOMMENDATION_CONSENSUS_POLICY", None))
    except ValueError:
        logger.warning("Invalid RECOMMENDATION_CONSENSUS_POLICY; using safest.")
        return ConsensusPolicy.SAFEST


#: One source output: its signal, its reconciliation unit (v1 source or source id) and,
#: for built-in sources, the v1 recommendation behind the signal.
SignalEntry = Tuple[SourceSignal, str, Optional[Recommendation]]


def entries_for_recommendations(
    recs: Sequence[Recommendation], cluster_uid: str = "default", fetched_at: Optional[datetime] = None
) -> List[SignalEntry]:
    """Signal entries of v1 recommendations (callers that only have v1 output)."""
    now = fetched_at or datetime.now(timezone.utc)
    entries: List[SignalEntry] = []
    for rec in recs:
        source = _source_value(rec)
        signal = signal_from_recommendation(rec, source_id=source, cluster_uid=cluster_uid, fetched_at=now)
        entries.append((signal, source, rec))
    return entries


def entries_for_reports(reports: Sequence["SourceReport"], cluster_uid: str = "default") -> List[SignalEntry]:
    """Signal entries of one scan's source reports.

    A built-in source (VPA, native analyzers, the NodePool heuristic) reports
    v1 recommendations and their signals: each recommendation is paired with
    its signal (same v1 fingerprint) so the consensus keeps the source's
    algorithm and engine family (``vpa-recommender``). The container-resources
    signals of a built-in source that back no v1 recommendation (one per VPA
    object: a Goldilocks VPA next to a user VPA, two VPAs that disagree, an
    increase) vote too, without a v1 base, under the source's reconciliation
    unit; their engine family keeps a VPA object seen twice to one vote. A
    signal-only source (third-party connector, pushed signals) contributes its
    signals alone, under its own source id: it becomes a recommendation only
    through consensus, and a plugin can never claim a built-in source's identity.
    """
    entries: List[SignalEntry] = []
    for report in reports:
        if report.recommendations:
            unit = report.legacy_source or report.source_id
            by_fingerprint: Dict[Tuple[Optional[str], str], SourceSignal] = {}
            for signal in report.signals:
                by_fingerprint.setdefault((signal.provenance.legacy_fingerprint, signal.kind), signal)
            fetched_at = report.run.finished_at
            paired: set = set()
            for rec in report.recommendations:
                key = (legacy_fingerprint(rec), _type_value(rec))
                signal = by_fingerprint.get(key)
                if signal is None:
                    signal = signal_from_recommendation(
                        rec, source_id=report.source_id, cluster_uid=cluster_uid, fetched_at=fetched_at
                    )
                paired.add(signal.signal_id)
                entries.append((signal, _source_value(rec) or unit, rec))
            for signal in report.signals:
                if signal.signal_id in paired or signal.capability not in _PER_OBJECT_CAPABILITIES:
                    continue
                paired.add(signal.signal_id)
                entries.append((signal, unit, None))
            continue
        for signal in report.signals:
            entries.append((signal, report.source_id, None))
    return entries


#: Capabilities whose built-in signals vote even without a v1 recommendation (per-VPA-object
#: container advice). Advisory signals of other capabilities (NodePool policy, G5) stay signals.
_PER_OBJECT_CAPABILITIES = frozenset({Capability.CONTAINER_REQUESTS, Capability.CONTAINER_LIMITS})


def consensus_refiner(context: Any) -> Refine:
    """Projects a consolidated request no built-in recommendation projected (same Impact Model, B4).

    A request change of a workload container is priced with the scan's
    :class:`~greenkube.impact.projector.ImpactProjector`; anything else keeps
    no projection rather than another value's.
    """
    state: Dict[str, Any] = {}

    def refine(rec: Recommendation) -> Recommendation:
        unprojected = rec.model_copy(
            update={"potential_savings_cost": None, "potential_savings_co2e_grams": None, "impact": None}
        )
        rec_type = _type_value(rec)
        resource = KIND_REQUEST_RESOURCES.get(rec_type)
        if resource is None or not rec.owner_kind or not rec.owner_name or context is None:
            return unprojected
        current_field, proposed_field = RESOURCE_FIELDS[resource]
        current, proposed = getattr(rec, current_field), getattr(rec, proposed_field)
        if current is None or proposed is None:
            return unprojected
        from greenkube.impact.projector import ImpactProjector, impact_fields

        projector = state.get("projector")
        if projector is None:
            projector = state["projector"] = ImpactProjector.for_context(context)
        target = (rec.namespace, rec.owner_kind, rec.owner_name)
        ref = projector.workload_ref(rec.namespace, rec.owner_kind, rec.owner_name, rec.container_name)
        rows = context.container_rows(target, rec.container_name)
        if resource == "cpu_request_m":
            delta = projector.container_resources(ref, rows, cpu_millicores=(current, proposed))
        else:
            delta = projector.container_resources(ref, rows, memory_bytes=(current, proposed))
        return rec.model_copy(update=impact_fields(delta))

    return refine


def track_record_weights(book: Optional["TrackRecordBook"]) -> Optional[Callable[[Ballot], float]]:
    """Weights of the ``weighted`` policy: each source's realization rate (Beta mean, I3)."""
    if book is None or not book.records:
        return None
    from greenkube.models.metrics import capability_for_type

    def weight(ballot: Ballot) -> float:
        try:
            capability = capability_for_type(RecommendationType(ballot.kind))
        except ValueError:
            return 1.0
        return book.get(ballot.source, capability).mean

    return weight


def apply_consensus_assessment(recs: Sequence[Recommendation], config: "Config") -> List[Recommendation]:
    """Puts each consensus in the evidence and rewards agreement between independent sources (E6).

    Agreement of at least 0.8 between two or more independent sources raises
    the confidence (capped at 1); the per-source spread is shown with the
    evidence. Recommendations whose confidence changed are scored again.
    """
    from greenkube.core.optimization.scoring import score_recommendations
    from greenkube.signals.consensus import Consensus

    result: List[Recommendation] = []
    rescore = False
    for rec in recs:
        if not isinstance(rec.consensus, dict):
            result.append(rec)
            continue
        try:
            consensus = Consensus.model_validate(rec.consensus)
        except ValueError:
            result.append(rec)
            continue
        update: Dict[str, Any] = {}
        evidence = rec.evidence
        bonus = consensus.confidence_bonus
        confidence = rec.confidence
        if bonus > 1.0 and confidence is not None:
            confidence = round(min(1.0, confidence * bonus), 4)
            update["confidence"] = confidence
            rescore = True
        if evidence is not None:
            evidence_update: Dict[str, Any] = {"consensus": rec.consensus}
            if "confidence" in update:
                evidence_update["confidence"] = confidence
                evidence_update["confidence_factors"] = {
                    **(evidence.confidence_factors or {}),
                    "consensus_agreement": bonus,
                }
            update["evidence"] = evidence.model_copy(update=evidence_update)
        result.append(rec.model_copy(update=update) if update else rec)
    if rescore:
        return score_recommendations(result, config=config)
    return result


def ballots_for_entries(entries: Sequence[SignalEntry]) -> List[Ballot]:
    ballots: List[Ballot] = []
    for signal, source, rec in entries:
        ballots.extend(
            ballots_for_signal(
                signal,
                source=source,
                payload=rec,
                payload_kind=_type_value(rec) if rec is not None else None,
            )
        )
    return ballots


def _with_value(rec: Recommendation, resource: Optional[str], value: Optional[int], current: Optional[int]):
    """``rec`` proposing ``value`` for ``resource``; True when the proposal changed."""
    if resource not in RESOURCE_FIELDS or value is None:
        return rec, False
    current_field, proposed_field = RESOURCE_FIELDS[resource]
    update: Dict[str, Any] = {}
    if getattr(rec, proposed_field) != value:
        update[proposed_field] = value
    if getattr(rec, current_field) is None and current is not None:
        update[current_field] = current
    if not update:
        return rec, False
    return rec.model_copy(update=update), proposed_field in update


def _signal_recommendation(ballot: Ballot) -> Optional[Recommendation]:
    """The v1 recommendation of a signal-only ballot, or None when its kind has no v1 type."""
    kind = ballot.kind
    try:
        RecommendationType(kind)
    except ValueError:
        logger.debug(
            "Signal %s of '%s' has no v1 finding kind (%s); kept as a signal.",
            ballot.signal.signal_id,
            ballot.source_id,
            kind,
        )
        return None
    signal = ballot.signal
    if signal.kind != kind:
        signal = signal.model_copy(update={"kind": kind})
    try:
        rec = recommendation_from_signal(signal)
    except ValueError:
        logger.debug(
            "Signal %s of '%s' cannot be expressed as a v1 recommendation.", signal.signal_id, ballot.source_id
        )
        return None
    if ballot.resource in KIND_REQUEST_RESOURCES.values():
        # A multi-resource proposal votes per resource: keep only this group's resource.
        for resource, (current_field, proposed_field) in RESOURCE_FIELDS.items():
            if resource != ballot.resource:
                rec = rec.model_copy(update={current_field: None, proposed_field: None})
    return rec


def _describe(rec: Recommendation, outcome: ConsensusOutcome) -> Recommendation:
    """Description and reason of a consolidated value no single built-in recommendation stated."""
    consensus = outcome.consensus
    resource = consensus.resource
    if resource not in RESOURCE_FIELDS or consensus.value is None:
        return rec
    current_field, _ = RESOURCE_FIELDS[resource]
    current = getattr(rec, current_field)
    label = "CPU request" if resource.startswith("cpu") else "memory request"
    target = f"{rec.owner_kind or 'Pod'} '{rec.owner_name or rec.pod_name or rec.namespace}'"
    if rec.container_name:
        target += f" container '{rec.container_name}'"
    if resource.startswith("cpu"):
        proposed = f"{consensus.value}m"
        before = f"{current}m" if current is not None else "unknown"
    else:
        proposed = f"{consensus.value / (1024 * 1024):.0f}MiB"
        before = f"{current / (1024 * 1024):.0f}MiB" if current is not None else "unknown"
    description = (
        f"{target} {label} {before} → {proposed}: {consensus.policy} consensus of "
        f"{consensus.independent_votes} source{'s' if consensus.independent_votes != 1 else ''}."
    )
    reason = f"Consensus across sources: {consensus.summary()}."
    if rec.reason:
        reason = f"{reason} {rec.reason}"
    return rec.model_copy(update={"description": description, "reason": reason})


def proposes_no_change(rec: Recommendation) -> bool:
    """True when a request recommendation proposes its current value (an empty change).

    Built-in analyzers only emit strict changes; a third-party source can report
    a value for every container, and a value floored to the minimum can land on
    the current request. Neither is advice.
    """
    resource = KIND_REQUEST_RESOURCES.get(_type_value(rec))
    if resource is None:
        return False
    current_field, proposed_field = RESOURCE_FIELDS[resource]
    current, proposed = getattr(rec, current_field), getattr(rec, proposed_field)
    return current is not None and proposed is not None and int(current) == int(proposed)


def materialize(outcome: ConsensusOutcome, refine: Optional[Refine] = None) -> Optional[Recommendation]:
    """The v1 recommendation of one consensus, or None when it has no v1 form."""
    chosen = outcome.chosen
    consensus = outcome.consensus
    base_ballot: Optional[Ballot] = chosen if chosen.payload is not None else None
    if base_ballot is None:
        with_payload = [ballot for ballot in outcome.representatives if ballot.payload is not None]
        with_payload += [ballot for ballot in outcome.ballots if ballot.payload is not None]
        base_ballot = with_payload[0] if with_payload else None

    if base_ballot is not None:
        rec: Optional[Recommendation] = base_ballot.payload
    else:
        rec = _signal_recommendation(chosen)
    if rec is None:
        return None

    rec, value_changed = _with_value(rec, consensus.resource, consensus.value, chosen.current)
    if proposes_no_change(rec):
        # A source that says "keep the current request" wins under safest: nothing to change.
        logger.debug("Consensus for %s keeps the current request; no recommendation.", outcome.key)
        return None
    restated = value_changed or base_ballot is not chosen
    if restated and refine is not None:
        try:
            rec = refine(rec)
        except Exception as exc:
            logger.warning("Could not project the consensus value of %s (%s).", outcome.key, type(exc).__name__)
            rec = rec.model_copy(
                update={"potential_savings_cost": None, "potential_savings_co2e_grams": None, "impact": None}
            )
    elif value_changed:
        # Never keep the projection of another value.
        rec = rec.model_copy(
            update={"potential_savings_cost": None, "potential_savings_co2e_grams": None, "impact": None}
        )
    if restated:
        rec = _describe(rec, outcome)

    update: Dict[str, Any] = {
        "source": coerce_recommendation_source(chosen.source),
        "sources": outcome.sources,
        "consensus": consensus.model_dump(mode="json"),
        "target": chosen.signal.target.model_dump(mode="json"),
    }
    if base_ballot is not chosen:
        update["source_ref"] = chosen.signal.source_ref
    if consensus.resource is None:
        proposal = chosen.signal.proposal
        if getattr(proposal, "kind", None) in ("hpa", "node_pool", "replicas") and base_ballot is not chosen:
            update["proposal"] = proposal.model_dump(mode="json")
    return rec.model_copy(update=update)


def consolidate_entries(
    entries: Sequence[SignalEntry],
    config: "Config",
    *,
    refine: Optional[Refine] = None,
    weight_for: Optional[Callable[[Ballot], float]] = None,
) -> List[Recommendation]:
    """One recommendation per target, capability and kind, by consensus across sources."""
    outcomes = consolidate(
        ballots_for_entries(entries),
        consensus_policy(config),
        priorities=_source_priority_map(config),
        weight_for=weight_for,
    )
    result: List[Recommendation] = []
    for outcome in outcomes:
        rec = materialize(outcome, refine)
        if rec is None:
            continue
        if outcome.consensus.independent_votes > 1:
            logger.debug("Consensus for %s: %s", outcome.key, outcome.consensus.summary())
        result.append(rec)
    return result


def deduplicate(recs: List[Recommendation]) -> List[Recommendation]:
    """Removes duplicate recommendations with the same target and type."""
    seen = set()
    result = []
    for rec in recs:
        key = (rec.scope, rec.namespace, rec.pod_name, rec.container_name or "", rec.type, rec.target_node or "")
        if key not in seen:
            seen.add(key)
            result.append(rec)
    return result


def finalize_entries(
    entries: Sequence[SignalEntry],
    config: "Config",
    *,
    refine: Optional[Refine] = None,
    weight_for: Optional[Callable[[Ballot], float]] = None,
) -> List[Recommendation]:
    """Consolidates by consensus, deduplicates and clamps values."""
    consolidated = consolidate_entries(entries, config, refine=refine, weight_for=weight_for)
    clamped = [apply_minimum_thresholds(rec, config) for rec in deduplicate(consolidated)]
    return [rec for rec in clamped if not proposes_no_change(rec)]


def finalize_recommendations(recs: List[Recommendation], config: "Config") -> List[Recommendation]:
    """Consolidates v1 recommendations by consensus, deduplicates and clamps values."""
    from greenkube.core.optimization.registry import cluster_uid

    return finalize_entries(entries_for_recommendations(recs, cluster_uid(config)), config)


#: Relative change of a proposed request below which a scan keeps the stored advice (hysteresis):
#: usage jitter between scans (byte-level working-set noise) must not create a new revision.
REVISION_HYSTERESIS = 0.05

#: Fields of a held recommendation taken from the stored record: the advice and everything derived
#: from its value (projection, wording, evidence and consensus), so the record stays self-consistent.
_HELD_FIELDS = (
    "recommended_cpu_request_millicores",
    "recommended_memory_request_bytes",
    "description",
    "reason",
    "priority",
    "potential_savings_cost",
    "potential_savings_co2e_grams",
    "impact",
    "evidence",
    "proposal",
    "consensus",
)


#: Absolute floor of the hysteresis band per resource (IMP-13): a percentile-based proposal of a
#: small request moves by more than 5% from one sample to the next (56m and 60m differ by 7%).
#: CPU uses ``RECOMMENDATION_MIN_CPU_MILLICORES`` (default 10m) when a config is given.
HYSTERESIS_FLOOR = {"cpu_request_m": 10, "memory_request_bytes": 16 * 1024 * 1024}

#: Consecutive scans that must propose a lower request, beyond the band, before it becomes a new
#: revision (IMP-13). A higher request (the safer direction) is accepted at once.
REVISION_CONFIRMATION_SCANS = 3


def _request_change(rec: Any, before: Any) -> Optional[Tuple[str, int, int, int]]:
    """``(resource, current, proposed, previous)`` when ``rec`` and ``before`` are comparable request advice.

    Comparable means: a request change of the same kind, against the same
    observed current request and in the same direction (both decreases or
    both increases). Anything else is new advice.
    """
    rec_type = _type_value(rec)
    resource = KIND_REQUEST_RESOURCES.get(rec_type)
    if resource is None or rec_type != source_text(getattr(before, "type", None)):
        return None
    current_field, proposed_field = RESOURCE_FIELDS[resource]
    current, previous_current = getattr(rec, current_field), getattr(before, current_field)
    proposed, previous = getattr(rec, proposed_field), getattr(before, proposed_field)
    if None in (current, previous_current, proposed, previous) or int(current) != int(previous_current):
        return None
    proposed, previous, current = int(proposed), int(previous), int(current)
    if previous <= 0 or (proposed > current) != (previous > current):
        return None
    return resource, current, proposed, previous


def hysteresis_band(
    resource: str, previous: int, threshold: float = REVISION_HYSTERESIS, floor: Optional[int] = None
) -> float:
    """Largest move from ``previous`` still held: ``max(threshold x previous, floor)`` (IMP-13)."""
    return max(threshold * previous, HYSTERESIS_FLOOR[resource] if floor is None else floor)


def within_hysteresis(
    rec: Any, before: Any, threshold: float = REVISION_HYSTERESIS, floor: Optional[int] = None
) -> bool:
    """True when ``rec`` restates ``before``'s request advice within the hysteresis band.

    Only a request change of the same kind, against the same observed current
    request and in the same direction (both decreases or both increases) is
    held; any other difference is new advice. The band is relative
    (``threshold``) with an absolute floor per resource (:data:`HYSTERESIS_FLOOR`).
    """
    change = _request_change(rec, before)
    if change is None:
        return False
    resource, _current, proposed, previous = change
    if proposed == previous:
        return False
    return abs(proposed - previous) <= hysteresis_band(resource, previous, threshold, floor)


def _hold(rec: Recommendation, before: RecommendationRecord, pending: Optional[dict]) -> Recommendation:
    update = {name: getattr(before, name) for name in _HELD_FIELDS if name in type(before).model_fields}
    update["pending_revision"] = pending
    return rec.model_copy(update=update)


def apply_revision_hysteresis(
    recs: Sequence[Recommendation],
    previous: Sequence[RecommendationRecord],
    threshold: float = REVISION_HYSTERESIS,
    config: Optional["Config"] = None,
) -> List[Recommendation]:
    """Holds the stored advice of every recommendation whose new proposal is not a confirmed change.

    A held recommendation takes the stored proposal and what is derived from it
    (:data:`_HELD_FIELDS`), so persisting it refreshes the record without a new
    revision (E7), and the typed action built afterwards for its workload bundle
    is the same for every member: a CPU and a memory recommendation of one
    container never carry two different copies of their shared action.

    IMP-13: a higher request (the safer direction) is held only within the
    relative ``threshold`` and is otherwise a new revision at once. A lower
    request is held within the band (``max(threshold x stored, floor)``, see
    :func:`hysteresis_band`) and becomes a new revision only when
    :data:`REVISION_CONFIRMATION_SCANS`
    consecutive scans proposed it (each within the band of the previous one).
    Until then the stored advice is held and ``pending_revision`` records the
    candidate and its count, so proposals that alternate between two values
    never flip the revision at every scan.
    """
    cpu_floor = getattr(config, "RECOMMENDATION_MIN_CPU_MILLICORES", None) if config is not None else None
    by_fingerprint = {record.fingerprint: record for record in previous if record.fingerprint}
    held: List[Recommendation] = []
    for rec in recs:
        fingerprint = legacy_fingerprint(rec)
        before = by_fingerprint.get(fingerprint) if fingerprint else None
        change = _request_change(rec, before) if before is not None else None
        if before is None or change is None:
            held.append(rec)
            continue
        resource, _current, proposed, stored = change
        floor = cpu_floor if resource == "cpu_request_m" else None
        if proposed == stored:
            held.append(rec)
            continue
        if proposed > stored:
            # A higher request is the safer direction: held only within the relative band, where
            # the stored value still clears the increase trigger (REC-03), otherwise taken at once.
            held.append(_hold(rec, before, None) if proposed - stored <= threshold * stored else rec)
            continue
        if stored - proposed <= hysteresis_band(resource, stored, threshold, floor):
            logger.debug("Holding %s %s: the new proposal is within the band.", _type_value(rec), before.fingerprint)
            held.append(_hold(rec, before, None))
            continue
        pending = before.pending_revision or {}
        candidate = pending.get("value")
        scans = 1
        if isinstance(candidate, int) and abs(proposed - candidate) <= hysteresis_band(
            resource, candidate, threshold, floor
        ):
            scans = int(pending.get("scans") or 0) + 1
        if scans >= REVISION_CONFIRMATION_SCANS:
            held.append(rec)
            continue
        logger.debug(
            "Holding %s %s: a lower request (%d) is waiting for confirmation (%d/%d scans).",
            _type_value(rec),
            before.fingerprint,
            proposed,
            scans,
            REVISION_CONFIRMATION_SCANS,
        )
        held.append(_hold(rec, before, {"value": proposed, "scans": scans}))
    return held


def legacy_fingerprint(rec: Recommendation) -> Optional[str]:
    """The v1 (v3 identity) fingerprint of a recommendation."""
    return RecommendationRecord.from_recommendation(rec).fingerprint


__all__ = [
    "HYSTERESIS_FLOOR",
    "REVISION_CONFIRMATION_SCANS",
    "REVISION_HYSTERESIS",
    "RecommendationSource",
    "SignalEntry",
    "apply_revision_hysteresis",
    "hysteresis_band",
    "consensus_policy",
    "consolidate_entries",
    "deduplicate",
    "entries_for_recommendations",
    "finalize_entries",
    "finalize_recommendations",
    "legacy_fingerprint",
    "materialize",
    "proposes_no_change",
    "within_hysteresis",
]
