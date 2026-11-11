"""Mapping between v1 recommendations and v2 signals (roadmap E4).

Analyzers and v1 connectors keep returning :class:`~greenkube.models.metrics.Recommendation`
objects; the SDK adapters turn each into a :class:`SourceSignal` with
:func:`signal_from_recommendation`. :func:`recommendation_from_signal` is the
reverse: the v1 flat fields are computed from the target and the proposal,
and only what cannot be derived (scope, priority...) travels in
``provenance.legacy_fields``. A v1 → v2 → v1 round trip is lossless.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    HpaProposal,
    ResourceSpec,
    ScheduleProposal,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
    TimeWindow,
    trim_raw,
)

#: v2 capability of each v1 recommendation type.
TYPE_CAPABILITY: Dict[str, Capability] = {
    RecommendationType.RIGHTSIZING_CPU.value: Capability.CONTAINER_REQUESTS,
    RecommendationType.RIGHTSIZING_MEMORY.value: Capability.CONTAINER_REQUESTS,
    RecommendationType.AUTOSCALING_CANDIDATE.value: Capability.HORIZONTAL_AUTOSCALING,
    RecommendationType.OFF_PEAK_SCALING.value: Capability.SCHEDULE,
    RecommendationType.ZOMBIE_POD.value: Capability.CLEANUP,
    RecommendationType.IDLE_NAMESPACE.value: Capability.CLEANUP,
    RecommendationType.ORPHANED_PERSISTENT_VOLUME.value: Capability.CLEANUP,
    RecommendationType.ORPHANED_LOAD_BALANCER.value: Capability.CLEANUP,
    RecommendationType.CARBON_AWARE_SCHEDULING.value: Capability.PLACEMENT,
    RecommendationType.OVERPROVISIONED_NODE.value: Capability.NODE_POOL,
    RecommendationType.UNDERUTILIZED_NODE.value: Capability.NODE_POOL,
}

#: v1 fields an adapter carries over from the source (enrichment fields such as
#: risk, ranking, eligibility and actions are computed later by the engine).
SOURCE_FIELDS = (
    "pod_name",
    "container_name",
    "namespace",
    "type",
    "description",
    "reason",
    "priority",
    "scope",
    "potential_savings_co2e_grams",
    "potential_savings_cost",
    "current_cpu_request_millicores",
    "recommended_cpu_request_millicores",
    "current_memory_request_bytes",
    "recommended_memory_request_bytes",
    "cron_schedule",
    "target_node",
    "source",
    "source_ref",
    "capability",
    "owner_kind",
    "owner_name",
    "impact",
)

_NODEPOOL_PREFIX = "nodepool/"
_SCHEDULE_TEXT = re.compile(r"^Scale to (\d+): (\d{2}:\d{2})-(\d{2}:\d{2}) ([A-Za-z_/+-]+)$")
_WORKLOAD_SCOPE_KINDS = frozenset({"Pod"})


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def capability_for(rec_type: str) -> Capability:
    """v2 capability of a v1 type; unknown types are hygiene findings."""
    return TYPE_CAPABILITY.get(str(_value(rec_type)), Capability.HYGIENE)


def target_for_recommendation(rec: Recommendation, cluster_uid: str) -> TargetRef:
    """Canonical target of a v1 recommendation."""
    rec_type = str(_value(rec.type))
    container = rec.container_name or None
    if rec.owner_kind and rec.owner_name:
        return TargetRef.for_kind(
            cluster_uid, rec.owner_kind, rec.owner_name, namespace=rec.namespace, container=container
        )
    if rec_type == RecommendationType.ORPHANED_PERSISTENT_VOLUME.value and rec.pod_name:
        return TargetRef.for_kind(cluster_uid, "PersistentVolume", rec.pod_name)
    if rec_type == RecommendationType.ORPHANED_LOAD_BALANCER.value and rec.pod_name:
        return TargetRef.for_kind(cluster_uid, "Service", rec.pod_name, namespace=rec.namespace)
    if rec.target_node:
        if rec.target_node.startswith(_NODEPOOL_PREFIX):
            return TargetRef.for_kind(cluster_uid, "NodePool", rec.target_node[len(_NODEPOOL_PREFIX) :])
        return TargetRef.for_kind(cluster_uid, "Node", rec.target_node)
    if rec.pod_name:
        return TargetRef.for_kind(cluster_uid, "Pod", rec.pod_name, namespace=rec.namespace, container=container)
    if rec.namespace:
        return TargetRef.for_kind(cluster_uid, "Namespace", rec.namespace)
    return TargetRef.for_kind(cluster_uid, "Cluster", cluster_uid)


def _schedule_proposal(text: Optional[str]) -> Optional[ScheduleProposal]:
    match = _SCHEDULE_TEXT.match(text or "")
    if match is None:
        return None
    replicas, sleep_at, wake_at, tz = match.groups()
    return ScheduleProposal(timezone=tz, sleep_at=sleep_at, wake_at=wake_at, replicas_while_asleep=int(replicas))


def proposal_for_recommendation(rec: Recommendation):
    """Typed v2 proposal of a v1 recommendation."""
    rec_type = str(_value(rec.type))
    capability = capability_for(rec_type)
    current = ResourceSpec(
        cpu_request_m=rec.current_cpu_request_millicores,
        memory_request_bytes=rec.current_memory_request_bytes,
    )
    if capability == Capability.CONTAINER_REQUESTS:
        return ContainerResourcesProposal(
            container=rec.container_name or None,
            current=current,
            proposed=ResourceSpec(
                cpu_request_m=rec.recommended_cpu_request_millicores,
                memory_request_bytes=rec.recommended_memory_request_bytes,
            ),
        )
    if capability == Capability.HORIZONTAL_AUTOSCALING:
        return HpaProposal(current=current if current != ResourceSpec() else None)
    if capability == Capability.SCHEDULE:
        schedule = _schedule_proposal(rec.cron_schedule)
        if schedule is not None:
            return schedule
        return AdvisoryProposal(text=rec.cron_schedule or rec.description)
    if capability == Capability.CLEANUP:
        return DeletionProposal(reason=rec.reason or rec.description)
    return AdvisoryProposal(text=rec.description)


def _derived_fields(signal: SourceSignal) -> Dict[str, Any]:
    """v1 fields computed from the target and the proposal alone."""
    target = signal.target
    rec_type = signal.kind
    fields: Dict[str, Any] = {
        "type": rec_type,
        "description": signal.evidence.summary,
        "reason": signal.evidence.details,
        "source_ref": signal.source_ref,
        "container_name": target.container,
        "namespace": target.namespace,
        "pod_name": None,
        "target_node": None,
        "owner_kind": None,
        "owner_name": None,
        "scope": "workload",
    }
    if signal.provenance.legacy_source:
        fields["source"] = signal.provenance.legacy_source
    if target.kind == "Namespace":
        fields.update(namespace=target.name, scope="namespace")
    elif target.kind == "Node":
        fields.update(target_node=target.name, scope="node")
    elif target.kind == "NodePool":
        fields.update(target_node=f"{_NODEPOOL_PREFIX}{target.name}", scope="node")
    elif target.kind in ("PersistentVolume", "Service"):
        fields.update(pod_name=target.name, scope="cluster")
    elif target.kind in _WORKLOAD_SCOPE_KINDS:
        fields.update(pod_name=target.name, scope="pod")
    elif target.kind != "Cluster":
        fields.update(pod_name=target.name, owner_kind=target.kind, owner_name=target.name)

    proposal = signal.proposal
    if isinstance(proposal, ContainerResourcesProposal):
        fields.update(
            current_cpu_request_millicores=proposal.current.cpu_request_m,
            recommended_cpu_request_millicores=proposal.proposed.cpu_request_m,
            current_memory_request_bytes=proposal.current.memory_request_bytes,
            recommended_memory_request_bytes=proposal.proposed.memory_request_bytes,
        )
    elif isinstance(proposal, HpaProposal) and proposal.current is not None:
        fields.update(
            current_cpu_request_millicores=proposal.current.cpu_request_m,
            current_memory_request_bytes=proposal.current.memory_request_bytes,
        )
    elif isinstance(proposal, ScheduleProposal):
        fields["cron_schedule"] = (
            f"Scale to {proposal.replicas_while_asleep}: {proposal.sleep_at}-{proposal.wake_at} {proposal.timezone}"
        )
    estimate = signal.source_estimate or {}
    for name in ("potential_savings_cost", "potential_savings_co2e_grams", "impact"):
        if name in estimate:
            fields[name] = estimate[name]
    return fields


def _comparable(rec: Recommendation) -> Dict[str, Any]:
    return rec.model_dump(mode="json", include=set(SOURCE_FIELDS))


def signal_from_recommendation(
    rec: Recommendation,
    *,
    source_id: str,
    cluster_uid: str,
    fetched_at: datetime,
    source_version: Optional[str] = None,
    algorithm: Optional[AlgorithmInfo] = None,
    observed_window: Optional[TimeWindow] = None,
    origin: SignalOrigin = SignalOrigin.NATIVE,
    raw: Optional[Dict[str, Any]] = None,
) -> SourceSignal:
    """Build the v2 signal of a v1 recommendation emitted by ``source_id``."""
    estimate = {
        name: value
        for name, value in (
            ("potential_savings_cost", rec.potential_savings_cost),
            ("potential_savings_co2e_grams", rec.potential_savings_co2e_grams),
            ("impact", rec.impact),
        )
        if value is not None
    }
    evidence_data: Dict[str, Any] = {}
    if rec.evidence is not None:
        evidence_data = rec.evidence.model_dump(mode="json")
    signal = SourceSignal(
        source_id=source_id,
        source_version=source_version,
        source_ref=rec.source_ref,
        target=target_for_recommendation(rec, cluster_uid),
        capability=capability_for(rec.type),
        kind=str(_value(rec.type)),
        proposal=proposal_for_recommendation(rec),
        algorithm=algorithm or AlgorithmInfo(name=source_id, window=observed_window),
        evidence=SignalEvidence(summary=rec.description, details=rec.reason, data=evidence_data),
        observed_window=observed_window,
        confidence=rec.confidence,
        source_estimate=estimate or None,
        provenance=SignalProvenance(
            origin=origin,
            legacy_source=str(_value(rec.source)),
            legacy_fingerprint=RecommendationRecord.from_recommendation(rec).fingerprint,
        ),
        raw=trim_raw(raw or {}),
        fetched_at=fetched_at,
    )
    # Keep only the v1 fields the target and proposal cannot reproduce.
    expected = _comparable(rec)
    rebuilt = _comparable(recommendation_from_signal(signal))
    legacy = {name: value for name, value in expected.items() if rebuilt.get(name) != value}
    if legacy:
        signal = signal.model_copy(
            update={"provenance": signal.provenance.model_copy(update={"legacy_fields": legacy})}
        )
    return signal


def recommendation_from_signal(signal: SourceSignal) -> Recommendation:
    """The v1 recommendation of a signal: flat fields from the proposal, then legacy overrides."""
    fields = _derived_fields(signal)
    fields.update(signal.provenance.legacy_fields)
    if signal.confidence is not None:
        fields.setdefault("confidence", signal.confidence)
    return Recommendation.model_validate(fields)
