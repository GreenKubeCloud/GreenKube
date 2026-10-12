"""Operator-approved, expiring manual workload binding resolutions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from greenkube.gitops.bindings.resolver import DEFAULT_BINDING_TTL, WorkloadBindingResolver
from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.storage.base_gitops_repository import WorkloadBinding, WorkloadBindingEvent


class ManualResolutionError(ValueError):
    """Base exception for a rejected manual binding resolution."""


class ManualResolutionConflictError(ManualResolutionError):
    """The workload is no longer in a manually actionable state."""


class ManualResolutionCandidateError(ManualResolutionError):
    """The requested candidate is not a current, selectable server candidate."""


class ManualResolutionStaleGenerationError(ManualResolutionError):
    """The selected source or controller changed after it was listed."""


@dataclass(frozen=True, slots=True)
class PreparedManualBindingResolution:
    """Validated binding and durable audit event ready for atomic persistence."""

    binding: WorkloadBinding
    event: WorkloadBindingEvent
    selected_candidate_id: str
    generation_token: str


def prepare_manual_binding_resolution(
    binding: WorkloadBinding,
    *,
    candidate_id: str,
    generation_token: str,
    actor: str,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_BINDING_TTL,
    max_ttl: timedelta = DEFAULT_BINDING_TTL,
) -> PreparedManualBindingResolution:
    """Build a manual override using only a current candidate already in the binding."""
    if binding.status not in {BindingStatus.AMBIGUOUS, BindingStatus.UNRESOLVED}:
        raise ManualResolutionConflictError("workload binding is not unresolved or ambiguous")
    if max_ttl <= timedelta(0) or ttl <= timedelta(0) or ttl > max_ttl:
        raise ValueError("manual binding TTL must be positive and no greater than the binding TTL")

    candidate = next((item for item in binding.candidates if item.id == candidate_id), None)
    if candidate is None or candidate.controller_kind is ControllerKind.MANUAL_RESOLUTION:
        raise ManualResolutionCandidateError("candidate is not part of the current binding")
    observed_at = _as_utc(now or datetime.now(timezone.utc))
    if candidate.status not in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS}:
        raise ManualResolutionCandidateError("candidate is not a current, selectable source")
    if candidate.provider_id == "unresolved" or candidate.repo_url == "unresolved":
        raise ManualResolutionCandidateError("candidate source is unresolved")
    if candidate.expires_at is not None and _as_utc(candidate.expires_at) <= observed_at:
        raise ManualResolutionCandidateError("candidate has expired")
    if observed_at - _as_utc(candidate.observed_at) >= ttl:
        raise ManualResolutionCandidateError("candidate is no longer current")

    current_generation = binding_candidate_generation_token(candidate)
    if generation_token != current_generation:
        raise ManualResolutionStaleGenerationError("candidate generation changed")

    expires_at = observed_at + ttl
    if candidate.expires_at is not None:
        expires_at = min(expires_at, _as_utc(candidate.expires_at))
    manual = _manual_source(
        candidate,
        generation_token=current_generation,
        observed_at=observed_at,
        expires_at=expires_at,
    )
    resolution_candidates = [manual if item.id == candidate.id else item for item in binding.candidates]
    resolved = WorkloadBindingResolver(freshness_ttl=ttl).resolve(resolution_candidates, now=observed_at)
    if resolved.status is not BindingStatus.RESOLVED or resolved.source is None or resolved.source.id != manual.id:
        raise ManualResolutionConflictError("manual source did not produce one resolved binding")

    audit_details = _audit_details(candidate)
    event = WorkloadBindingEvent(
        cluster=binding.cluster,
        namespace=binding.namespace,
        workload_kind=binding.workload_kind,
        workload_name=binding.workload_name,
        event_type="manual_resolution_created",
        actor=_safe_actor(actor),
        source_id=manual.id,
        source_generation=current_generation,
        expires_at=expires_at,
        details={
            **audit_details,
            "selected_candidate_id": candidate.id,
        },
        created_at=observed_at,
    )
    return PreparedManualBindingResolution(
        binding=resolved,
        event=event,
        selected_candidate_id=candidate.id,
        generation_token=current_generation,
    )


def manual_resolution_event(
    source: WorkloadSource,
    *,
    event_type: str,
    reason_code: str | None = None,
    now: datetime | None = None,
) -> WorkloadBindingEvent:
    """Create a safe, idempotent lifecycle audit event for a manual override."""
    snapshot = _manual_snapshot(source)
    selected_candidate_id = snapshot.get("selected_candidate_id")
    generation_token = snapshot.get("generation_token")
    details: dict[str, object] = {
        "selected_candidate_id": selected_candidate_id,
        "controller_kind": snapshot.get("controller_kind"),
        "controller_uid": snapshot.get("controller_uid"),
        "controller_generation": snapshot.get("controller_generation"),
        "source_uid": snapshot.get("source_uid"),
        "source_generation": snapshot.get("source_generation"),
        "source_revision": snapshot.get("source_revision"),
    }
    if reason_code is not None:
        details["reason_code"] = reason_code
    return WorkloadBindingEvent(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
        event_type=event_type,
        actor="system",
        source_id=source.id,
        source_generation=generation_token if isinstance(generation_token, str) else None,
        expires_at=source.expires_at,
        details=details,
        created_at=_as_utc(now or datetime.now(timezone.utc)),
    )


def manual_resolution_snapshot(source: WorkloadSource) -> Mapping[str, object]:
    """Return the persisted identity snapshot for a manual candidate."""
    return _manual_snapshot(source)


def _manual_source(
    candidate: WorkloadSource,
    *,
    generation_token: str,
    observed_at: datetime,
    expires_at: datetime,
) -> WorkloadSource:
    source_ref = candidate.evidence.get("source_ref")
    if isinstance(source_ref, Mapping):
        source_ref = {
            key: source_ref[key] for key in ("kind", "name", "namespace") if isinstance(source_ref.get(key), str)
        }
    else:
        source_ref = None
    snapshot = {
        "selected_candidate_id": candidate.id,
        "generation_token": generation_token,
        "controller_kind": candidate.controller_kind.value,
        "controller_name": candidate.controller_name,
        "controller_namespace": candidate.controller_namespace,
        "controller_uid": candidate.controller_uid,
        "controller_generation": candidate.controller_generation,
        "source_uid": candidate.source_uid,
        "source_generation": candidate.source_generation,
        "source_revision": candidate.source_revision,
        "source_ref": source_ref,
    }
    evidence = deepcopy(candidate.evidence)
    evidence["selected_via"] = "manual"
    evidence["manual_resolution"] = snapshot
    return WorkloadSource(
        id=candidate.id,
        cluster=candidate.cluster,
        namespace=candidate.namespace,
        workload_kind=candidate.workload_kind,
        workload_name=candidate.workload_name,
        provider_id=candidate.provider_id,
        repo_url=candidate.repo_url,
        ref=candidate.ref,
        revision=candidate.revision,
        path=candidate.path,
        controller_kind=candidate.controller_kind,
        controller_name=candidate.controller_name,
        controller_namespace=candidate.controller_namespace,
        controller_uid=candidate.controller_uid,
        controller_generation=candidate.controller_generation,
        source_uid=candidate.source_uid,
        source_generation=candidate.source_generation,
        source_revision=candidate.source_revision,
        renderer=candidate.renderer,
        renderer_config=deepcopy(candidate.renderer_config),
        confidence=1.0,
        status=BindingStatus.RESOLVED,
        evidence=evidence,
        observed_at=observed_at,
        expires_at=expires_at,
    )


def _manual_snapshot(source: WorkloadSource) -> Mapping[str, object]:
    snapshot = source.evidence.get("manual_resolution")
    return snapshot if isinstance(snapshot, Mapping) else {}


def _audit_details(candidate: WorkloadSource) -> dict[str, object]:
    return {
        "controller_kind": candidate.controller_kind.value,
        "controller_uid": candidate.controller_uid,
        "controller_generation": candidate.controller_generation,
        "source_uid": candidate.source_uid,
        "source_generation": candidate.source_generation,
        "source_revision": candidate.source_revision,
    }


def _safe_actor(value: str) -> str:
    normalized = value.strip()[:80]
    return normalized if re.fullmatch(r"[A-Za-z0-9@._:-]{1,80}", normalized) else "operator"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
