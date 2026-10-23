"""Priority-aware resolution of discovered workload source candidates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TypeAlias

from greenkube.gitops.contracts import DEFAULT_BINDING_TTL, BindingStatus, ControllerKind, RendererKind, WorkloadSource
from greenkube.storage.base_gitops_repository import WorkloadBinding

WorkloadIdentity: TypeAlias = tuple[str, str, str, str]
SourceUnit: TypeAlias = tuple[str, str, str | None, str, RendererKind]
SOURCE_PRECEDENCE: dict[ControllerKind, int] = {
    ControllerKind.MANUAL_RESOLUTION: 0,
    ControllerKind.ARGOCD: 1,
    ControllerKind.FLUX_KUSTOMIZATION: 1,
    ControllerKind.FLUX_HELM_RELEASE: 2,
    ControllerKind.EXPLICIT_REPOSITORY: 3,
    ControllerKind.ANNOTATION: 4,
}

_DUPLICATE_STATUS_PRIORITY = {
    BindingStatus.STALE: 0,
    BindingStatus.UNRESOLVED: 1,
    BindingStatus.BLOCKED: 2,
    BindingStatus.RESOLVED: 3,
    BindingStatus.AMBIGUOUS: 4,
}


def workload_identity(source: WorkloadSource) -> WorkloadIdentity:
    """Return the stable identity used to group candidates for one workload."""
    return source.cluster, source.namespace, source.workload_kind, source.workload_name


def source_unit(source: WorkloadSource) -> SourceUnit:
    """Return the source-unit tuple used to compare candidate ownership."""
    return source.provider_id, source.repo_url, source.revision, source.path, source.renderer


def group_workload_sources(
    candidates: Iterable[WorkloadSource],
) -> dict[WorkloadIdentity, list[WorkloadSource]]:
    """Group candidates by cluster and Kubernetes workload identity."""
    grouped: dict[WorkloadIdentity, list[WorkloadSource]] = defaultdict(list)
    for candidate in candidates:
        grouped[workload_identity(candidate)].append(candidate)
    return dict(grouped)


class WorkloadBindingResolver:
    """Resolve candidate source units without guessing across equal priorities."""

    def __init__(self, *, freshness_ttl: timedelta | None = DEFAULT_BINDING_TTL) -> None:
        if freshness_ttl is not None and freshness_ttl <= timedelta(0):
            raise ValueError("freshness_ttl must be greater than zero")
        self._freshness_ttl = freshness_ttl

    def resolve(
        self,
        candidates: Iterable[WorkloadSource],
        *,
        now: datetime | None = None,
    ) -> WorkloadBinding:
        """Resolve the candidates for one workload, retaining every candidate."""
        items = list(candidates)
        observed_at = _as_utc(now or datetime.now(timezone.utc))
        if not items:
            raise ValueError("at least one workload source candidate is required")

        identities = {workload_identity(candidate) for candidate in items}
        if len(identities) != 1:
            raise ValueError("workload source candidates must identify the same workload")
        cluster, namespace, workload_kind, workload_name = next(iter(identities))

        unique, _ = self._deduplicate(items)
        evaluated = [self._mark_stale_if_expired(candidate, observed_at) for candidate in unique]
        evaluated.sort(key=_candidate_sort_key)
        active = [candidate for candidate in evaluated if candidate.status is not BindingStatus.STALE]
        evidence = self._base_evidence(evaluated)

        if not active:
            stale_source = max(evaluated, key=_representative_sort_key)
            evidence["resolution"] = "no-fresh-candidates"
            return WorkloadBinding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.STALE,
                confidence=stale_source.confidence,
                source=stale_source,
                candidates=tuple(evaluated),
                evidence=evidence,
                reason="all-source-candidates-stale",
                observed_at=max(_as_utc(candidate.observed_at) for candidate in evaluated),
                expires_at=self._expiry(stale_source),
            )

        highest_priority = min(_source_precedence(candidate) for candidate in active)
        top = [candidate for candidate in active if _source_precedence(candidate) == highest_priority]
        top_units = {
            source_unit(candidate)
            for candidate in top
            if candidate.status in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS}
        }
        top.sort(key=_representative_sort_key, reverse=True)
        representative = top[0]
        evidence["priority"] = highest_priority
        evidence["top_candidate_ids"] = [candidate.id for candidate in top]
        evidence["provenance"] = []

        if len(top_units) > 1:
            evidence["resolution"] = "conflicting-equal-priority-source-units"
            evidence["conflicting_source_units"] = [
                _source_unit_data(unit) for unit in sorted(top_units, key=_source_unit_sort_key)
            ]
            return self._binding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.AMBIGUOUS,
                candidates=evaluated,
                evidence=evidence,
                reason="conflicting-equal-priority-source-units",
                confidence=max(candidate.confidence for candidate in top),
                observed_at=max(_as_utc(candidate.observed_at) for candidate in top),
                expires_at=self._expiry(representative),
            )

        if any(candidate.status is BindingStatus.AMBIGUOUS for candidate in top):
            evidence["resolution"] = "ambiguous-source-candidate"
            return self._binding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.AMBIGUOUS,
                candidates=evaluated,
                evidence=evidence,
                reason=_reason(top, BindingStatus.AMBIGUOUS, "highest-priority-source-is-ambiguous"),
                confidence=max(candidate.confidence for candidate in top),
                observed_at=max(_as_utc(candidate.observed_at) for candidate in top),
                expires_at=self._expiry(representative),
            )

        if any(candidate.status is BindingStatus.BLOCKED for candidate in top):
            evidence["resolution"] = "highest-priority-source-blocked"
            return self._binding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.BLOCKED,
                candidates=evaluated,
                evidence=evidence,
                reason=_reason(top, BindingStatus.BLOCKED, "highest-priority-source-blocked"),
                confidence=max(candidate.confidence for candidate in top),
                observed_at=max(_as_utc(candidate.observed_at) for candidate in top),
                expires_at=self._expiry(representative),
            )

        if any(candidate.status is BindingStatus.UNRESOLVED for candidate in top):
            evidence["resolution"] = "highest-priority-source-unresolved"
            return self._binding(
                cluster=cluster,
                namespace=namespace,
                workload_kind=workload_kind,
                workload_name=workload_name,
                status=BindingStatus.UNRESOLVED,
                candidates=evaluated,
                evidence=evidence,
                reason=_reason(top, BindingStatus.UNRESOLVED, "highest-priority-source-unresolved"),
                confidence=max(candidate.confidence for candidate in top),
                observed_at=max(_as_utc(candidate.observed_at) for candidate in top),
                expires_at=self._expiry(representative),
            )

        selected_unit = source_unit(representative)
        merged = [
            candidate
            for candidate in active
            if source_unit(candidate) == selected_unit and candidate.status is BindingStatus.RESOLVED
        ]
        selected = max(
            (
                candidate
                for candidate in top
                if source_unit(candidate) == selected_unit and candidate.status is BindingStatus.RESOLVED
            ),
            key=_representative_sort_key,
        )
        evidence["resolution"] = "resolved"
        evidence["source_unit"] = _source_unit_data(selected_unit)
        evidence["provenance"] = [_provenance(candidate) for candidate in merged]
        evidence["merged_source_ids"] = [candidate.id for candidate in merged]
        return self._binding(
            cluster=cluster,
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
            status=BindingStatus.RESOLVED,
            source=selected,
            candidates=evaluated,
            evidence=evidence,
            reason=None,
            confidence=selected.confidence,
            observed_at=max(_as_utc(candidate.observed_at) for candidate in merged),
            expires_at=self._expiry(selected),
        )

    def resolve_many(
        self,
        candidates: Iterable[WorkloadSource],
        *,
        now: datetime | None = None,
    ) -> dict[WorkloadIdentity, WorkloadBinding]:
        """Resolve a candidate collection into one binding per workload."""
        grouped = group_workload_sources(candidates)
        return {identity: self.resolve(grouped[identity], now=now) for identity in sorted(grouped)}

    def _deduplicate(
        self,
        candidates: list[WorkloadSource],
    ) -> tuple[list[WorkloadSource], set[str]]:
        by_id: dict[str, WorkloadSource] = {}
        source_units_by_id: dict[str, set[SourceUnit]] = defaultdict(set)
        for candidate in candidates:
            source_units_by_id[candidate.id].add(source_unit(candidate))
            existing = by_id.get(candidate.id)
            if existing is None or _duplicate_sort_key(candidate) > _duplicate_sort_key(existing):
                by_id[candidate.id] = candidate

        conflicting_ids = {candidate_id for candidate_id, units in source_units_by_id.items() if len(units) > 1}
        for candidate_id in conflicting_ids:
            candidate = by_id[candidate_id]
            evidence = dict(candidate.evidence)
            evidence["candidate_id_conflict"] = [
                _source_unit_data(unit) for unit in sorted(source_units_by_id[candidate_id], key=_source_unit_sort_key)
            ]
            status = BindingStatus.AMBIGUOUS if candidate.status is not BindingStatus.STALE else BindingStatus.STALE
            by_id[candidate_id] = candidate.model_copy(
                update={
                    "status": status,
                    "reason": "duplicate-candidate-id-conflict",
                    "evidence": evidence,
                }
            )
        return list(by_id.values()), conflicting_ids

    def _mark_stale_if_expired(self, candidate: WorkloadSource, now: datetime) -> WorkloadSource:
        if candidate.status is BindingStatus.STALE:
            return candidate
        observed_at = _as_utc(candidate.observed_at)
        is_expired = candidate.expires_at is not None and _as_utc(candidate.expires_at) <= now
        is_too_old = self._freshness_ttl is not None and now - observed_at >= self._freshness_ttl
        if not is_expired and not is_too_old:
            return candidate
        reason = "source-expired" if is_expired else "source-observation-expired"
        evidence = dict(candidate.evidence)
        evidence["stale_reason"] = reason
        return candidate.model_copy(
            update={
                "status": BindingStatus.STALE,
                "reason": candidate.reason or reason,
                "evidence": evidence,
            }
        )

    def _base_evidence(self, candidates: list[WorkloadSource]) -> dict[str, object]:
        return {
            "candidate_count": len(candidates),
            "candidate_ids": [candidate.id for candidate in candidates],
            "provenance": [_provenance(candidate) for candidate in candidates],
        }

    def _expiry(self, candidate: WorkloadSource) -> datetime | None:
        if candidate.expires_at is not None:
            return _as_utc(candidate.expires_at)
        if self._freshness_ttl is None:
            return None
        return _as_utc(candidate.observed_at) + self._freshness_ttl

    def _binding(
        self,
        *,
        cluster: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        status: BindingStatus,
        candidates: list[WorkloadSource],
        evidence: dict[str, object],
        reason: str | None,
        confidence: float,
        observed_at: datetime,
        expires_at: datetime | None,
        source: WorkloadSource | None = None,
    ) -> WorkloadBinding:
        return WorkloadBinding(
            cluster=cluster,
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
            status=status,
            confidence=confidence,
            source=source,
            candidates=tuple(candidates),
            evidence=evidence,
            reason=reason,
            observed_at=observed_at,
            expires_at=expires_at,
        )


def _reason(
    candidates: list[WorkloadSource],
    status: BindingStatus,
    fallback: str,
) -> str:
    return next(
        (candidate.reason for candidate in candidates if candidate.status is status and candidate.reason),
        fallback,
    )


def _provenance(candidate: WorkloadSource) -> dict[str, object]:
    return {
        "source_id": candidate.id,
        "controller_kind": candidate.controller_kind.value,
        "controller_name": candidate.controller_name,
        "controller_namespace": candidate.controller_namespace,
        "evidence": candidate.evidence,
    }


def _source_unit_data(unit: SourceUnit) -> dict[str, object]:
    provider_id, repo_url, revision, path, renderer = unit
    return {
        "provider_id": provider_id,
        "repo_url": repo_url,
        "revision": revision,
        "path": path,
        "renderer": renderer.value,
    }


def _source_unit_sort_key(unit: SourceUnit) -> tuple[str, str, bool, str, str, str]:
    provider_id, repo_url, revision, path, renderer = unit
    return provider_id, repo_url, revision is not None, revision or "", path, renderer.value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _representative_sort_key(candidate: WorkloadSource) -> tuple[float, float, str]:
    return candidate.confidence, _as_utc(candidate.observed_at).timestamp(), candidate.id


def _candidate_sort_key(candidate: WorkloadSource) -> tuple[int, int, float, float, str]:
    return (
        1 if candidate.status is BindingStatus.STALE else 0,
        _source_precedence(candidate),
        -candidate.confidence,
        -_as_utc(candidate.observed_at).timestamp(),
        candidate.id,
    )


def _source_precedence(candidate: WorkloadSource) -> int:
    if candidate.controller_kind is ControllerKind.MANUAL_RESOLUTION:
        return SOURCE_PRECEDENCE[ControllerKind.MANUAL_RESOLUTION]
    if candidate.evidence.get("selected_via") == "manual":
        return -1
    return SOURCE_PRECEDENCE[candidate.controller_kind]


def _duplicate_sort_key(candidate: WorkloadSource) -> tuple[int, int, float, int, float]:
    return (
        0 if candidate.status is BindingStatus.STALE else 1,
        1 if candidate.evidence.get("selected_via") == "manual" else 0,
        _as_utc(candidate.observed_at).timestamp(),
        _DUPLICATE_STATUS_PRIORITY[candidate.status],
        candidate.confidence,
    )
