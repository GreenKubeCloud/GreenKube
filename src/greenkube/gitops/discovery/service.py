"""Binding refresh and periodic reconciliation for Kubernetes GitOps discovery."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from greenkube.gitops.bindings.manual import manual_resolution_event, manual_resolution_snapshot
from greenkube.gitops.bindings.resolver import (
    WorkloadBindingResolver,
    WorkloadIdentity,
    group_workload_sources,
)
from greenkube.gitops.contracts import (
    DEFAULT_BINDING_TTL,
    BindingStatus,
    ControllerKind,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.gitops.discovery.kubernetes import CapabilityAvailability, KubernetesDiscoveryResult
from greenkube.storage.base_gitops_repository import (
    MAX_WORKLOAD_BINDING_CANDIDATES,
    WorkloadBinding,
    WorkloadBindingCoverage,
    WorkloadBindingRepository,
)

DEFAULT_DISCOVERY_INTERVAL_SECONDS = 300.0
MIN_DISCOVERY_INTERVAL_SECONDS = 1.0
MAX_DISCOVERY_INTERVAL_SECONDS = 3600.0

_CONTROLLER_CAPABILITIES = {
    ControllerKind.ARGOCD: "argocd",
    ControllerKind.FLUX_KUSTOMIZATION: "flux_kustomization",
    ControllerKind.FLUX_HELM_RELEASE: "flux_helm_release",
}
_FLUX_SOURCE_CAPABILITIES = {
    "Bucket": "flux_source_bucket",
    "GitRepository": "flux_source_git_repository",
    "HelmRepository": "flux_source_helm_repository",
    "HelmChart": "flux_source_helm_chart",
    "OCIRepository": "flux_source_oci_repository",
}
_DISCOVERY_CAPABILITIES = (
    "argocd",
    "flux_kustomization",
    "flux_helm_release",
    "flux_source_git_repository",
    "flux_source_oci_repository",
    "flux_source_helm_repository",
    "flux_source_bucket",
    "flux_source_helm_chart",
)


class _DiscoverySource(Protocol):
    async def discover(
        self,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> KubernetesDiscoveryResult: ...


@dataclass(frozen=True, slots=True)
class DiscoveryCoverage:
    """Binding totals by resolution status."""

    total: int = 0
    resolved: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    blocked: int = 0
    stale: int = 0

    @classmethod
    def from_bindings(cls, bindings: Iterable[WorkloadBinding]) -> DiscoveryCoverage:
        counts = {status: 0 for status in BindingStatus}
        total = 0
        for binding in bindings:
            total += 1
            counts[binding.status] += 1
        return cls(
            total=total,
            resolved=counts[BindingStatus.RESOLVED],
            ambiguous=counts[BindingStatus.AMBIGUOUS],
            unresolved=counts[BindingStatus.UNRESOLVED],
            blocked=counts[BindingStatus.BLOCKED],
            stale=counts[BindingStatus.STALE],
        )

    @classmethod
    def from_projection(cls, coverage: WorkloadBindingCoverage) -> DiscoveryCoverage:
        return cls(
            total=coverage.total,
            resolved=coverage.resolved,
            ambiguous=coverage.ambiguous,
            unresolved=coverage.unresolved,
            blocked=coverage.blocked,
            stale=coverage.stale,
        )

    @property
    def ratio(self) -> float:
        """Return the resolved fraction; an empty inventory has zero coverage."""
        return self.resolved / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class DiscoveryRefreshResult:
    """Bindings, capability metadata, and coverage produced by one refresh."""

    refreshed_at: datetime
    bindings: tuple[WorkloadBinding, ...]
    coverage: DiscoveryCoverage
    availability: Mapping[str, CapabilityAvailability]


class GitOpsDiscoveryService:
    """Reconcile Kubernetes discovery candidates into durable workload bindings."""

    def __init__(
        self,
        discovery_source: _DiscoverySource,
        repository: WorkloadBindingRepository,
        *,
        cluster: str,
        provider_id: str,
        namespace: str | None = None,
        resolver: WorkloadBindingResolver | None = None,
        binding_ttl: timedelta = DEFAULT_BINDING_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not cluster.strip() or not provider_id.strip():
            raise ValueError("cluster and provider_id must not be blank")
        if binding_ttl <= timedelta(0):
            raise ValueError("binding_ttl must be positive")
        self._discovery_source = discovery_source
        self._repository = repository
        self._cluster = cluster
        self._provider_id = provider_id
        self._namespace = namespace
        self._binding_ttl = binding_ttl
        self._resolver = resolver or WorkloadBindingResolver(freshness_ttl=binding_ttl)
        self._clock = clock or _utc_now

    async def refresh(
        self,
        *,
        resources: Iterable[Mapping[str, Any]] = (),
    ) -> DiscoveryRefreshResult:
        """Discover, resolve, and persist current bindings and stale history."""
        discovery = await self._discovery_source.discover(
            cluster=self._cluster,
            provider_id=self._provider_id,
            namespace=self._namespace,
            resources=resources,
        )
        refreshed_at = _as_utc(self._clock())
        current = group_workload_sources(discovery.candidates)
        if any(identity[0] != self._cluster for identity in current):
            raise ValueError("discovery returned a candidate for a different cluster")

        persisted_bindings: dict[tuple[str, str, str, str], WorkloadBinding] = {}
        visited: set[tuple[str, str, str, str]] = set()
        cursor: str | None = None
        while True:
            page = await self._repository.list_workload_bindings_page(
                cluster=self._cluster,
                limit=100,
                cursor=cursor,
                candidate_limit=MAX_WORKLOAD_BINDING_CANDIDATES,
                now=refreshed_at,
            )
            for old_binding in page.items:
                identity = _binding_identity(old_binding)
                visited.add(identity)
                current_candidates = current.get(identity, [])
                events = ()
                if old_binding.candidates_truncated:
                    persisted_bindings[identity] = old_binding
                    continue
                if not current_candidates and not old_binding.candidates:
                    if old_binding.status is BindingStatus.STALE or not _all_capabilities_available(
                        discovery.availability,
                        namespace=self._namespace,
                    ):
                        binding = old_binding
                    else:
                        binding = _stale_binding_without_candidates(old_binding)
                else:
                    candidates, manual_events = _merge_candidates(
                        old_binding,
                        current_candidates,
                        discovery.availability,
                        namespace=self._namespace,
                        now=refreshed_at,
                        binding_ttl=self._binding_ttl,
                    )
                    events = tuple(manual_events)
                    binding = self._resolver.resolve(candidates, now=refreshed_at)
                    binding = _preserve_resolution_history(binding, old_binding)

                if binding == old_binding and not events:
                    saved = old_binding
                else:
                    saved = await self._repository.save_discovered_workload_binding(
                        binding,
                        expected_generation=old_binding.generation,
                        events=events,
                    )
                if saved is None:
                    saved = await self._repository.get_workload_binding(*identity)
                if saved is not None:
                    persisted_bindings[identity] = saved

            if not page.has_more:
                break
            cursor = page.next_cursor
            if cursor is None:
                raise RuntimeError("workload binding page is missing its continuation cursor")

        for identity in sorted(current):
            if identity in visited:
                continue
            candidates = current[identity]
            binding = self._resolver.resolve(candidates, now=refreshed_at)
            saved = await self._repository.save_discovered_workload_binding(
                binding,
                expected_generation=None,
            )
            if saved is None:
                saved = await self._repository.get_workload_binding(*identity)
            if saved is not None:
                persisted_bindings[identity] = saved

        coverage = DiscoveryCoverage.from_projection(
            await self._repository.get_workload_binding_coverage(
                cluster=self._cluster,
                now=refreshed_at,
            )
        )
        bindings = tuple(persisted_bindings[identity] for identity in sorted(persisted_bindings))
        return DiscoveryRefreshResult(
            refreshed_at=refreshed_at,
            bindings=bindings,
            coverage=coverage,
            availability=dict(discovery.availability),
        )

    async def coverage(self) -> DiscoveryCoverage:
        """Return freshness-aware binding counts for the configured cluster."""
        return DiscoveryCoverage.from_projection(
            await self._repository.get_workload_binding_coverage(cluster=self._cluster)
        )

    async def run_periodic(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        interval_seconds: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    ) -> None:
        """Refresh immediately, then repeat until stopped or the task is cancelled."""
        _validate_interval(interval_seconds)
        stop = stop_event if stop_event is not None else asyncio.Event()
        while not stop.is_set():
            await self.refresh()
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue


def _merge_candidates(
    previous: WorkloadBinding,
    current: list[WorkloadSource],
    availability: Mapping[str, CapabilityAvailability],
    *,
    namespace: str | None,
    now: datetime,
    binding_ttl: timedelta = DEFAULT_BINDING_TTL,
) -> tuple[list[WorkloadSource], list[Any]]:
    current_ids = {candidate.id for candidate in current}
    historical: list[WorkloadSource] = []
    manual_events = []
    for candidate in previous.candidates:
        if _is_manual_resolution(candidate):
            expires_at = candidate.expires_at
            if expires_at is None:
                stale_reason = "manual-resolution-expiry-missing"
                if candidate.id not in current_ids:
                    historical.append(_mark_manual_resolution_stale(candidate, stale_reason))
                manual_events.append(
                    manual_resolution_event(
                        candidate,
                        event_type="manual_resolution_invalidated",
                        reason_code=stale_reason,
                        now=now,
                    )
                )
                continue
            if _as_utc(expires_at) <= now or now - _as_utc(candidate.observed_at) >= binding_ttl:
                if candidate.id not in current_ids:
                    historical.append(_mark_manual_resolution_stale(candidate, "manual-resolution-expired"))
                manual_events.append(
                    manual_resolution_event(
                        candidate,
                        event_type="manual_resolution_expired",
                        reason_code="manual-resolution-expired",
                        now=now,
                    )
                )
                continue
            if candidate.status is BindingStatus.STALE:
                if candidate.id not in current_ids:
                    historical.append(candidate)
                continue

            snapshot = manual_resolution_snapshot(candidate)
            selected_candidate_id = snapshot.get("selected_candidate_id")
            selected_generation = snapshot.get("generation_token")
            selected = (
                next((item for item in current if item.id == selected_candidate_id), None)
                if isinstance(selected_candidate_id, str)
                else None
            )
            if selected is not None:
                is_current = (
                    selected.status in {BindingStatus.RESOLVED, BindingStatus.AMBIGUOUS}
                    and (selected.expires_at is None or _as_utc(selected.expires_at) > now)
                    and now - _as_utc(selected.observed_at) < binding_ttl
                    and isinstance(selected_generation, str)
                    and binding_candidate_generation_token(selected) == selected_generation
                )
                if is_current:
                    historical.append(candidate)
                    continue
                invalidation_reason = "manual-resolution-source-changed"
            elif _manual_controller_available(candidate, availability, namespace=namespace):
                invalidation_reason = "manual-resolution-source-changed"
            else:
                historical.append(candidate)
                continue

            if candidate.id not in current_ids:
                historical.append(_mark_manual_resolution_stale(candidate, invalidation_reason))
            manual_events.append(
                manual_resolution_event(
                    candidate,
                    event_type="manual_resolution_invalidated",
                    reason_code=invalidation_reason,
                    now=now,
                )
            )
            continue

        if candidate.id in current_ids:
            continue
        if candidate.status is BindingStatus.STALE:
            historical.append(candidate)
        elif _candidate_capabilities_available(candidate, availability, namespace=namespace):
            historical.append(_mark_candidate_stale(candidate))
        else:
            historical.append(candidate)
    return [*historical, *current], manual_events


def _is_manual_resolution(candidate: WorkloadSource) -> bool:
    return (
        candidate.controller_kind is ControllerKind.MANUAL_RESOLUTION
        or candidate.evidence.get("selected_via") == "manual"
    )


def _manual_controller_available(
    candidate: WorkloadSource,
    availability: Mapping[str, CapabilityAvailability],
    *,
    namespace: str | None,
) -> bool:
    snapshot = manual_resolution_snapshot(candidate)
    controller_value = snapshot.get("controller_kind")
    if not isinstance(controller_value, str):
        return False
    try:
        controller_kind = ControllerKind(controller_value)
    except ValueError:
        return False
    source_ref = snapshot.get("source_ref")
    evidence = {"source_ref": source_ref} if isinstance(source_ref, Mapping) else {}
    original = candidate.model_copy(update={"controller_kind": controller_kind, "evidence": evidence})
    return _candidate_capabilities_available(original, availability, namespace=namespace)


def _mark_manual_resolution_stale(candidate: WorkloadSource, reason: str) -> WorkloadSource:
    evidence = dict(candidate.evidence)
    evidence["stale_reason"] = reason
    return candidate.model_copy(
        update={
            "status": BindingStatus.STALE,
            "reason": reason,
            "evidence": evidence,
        }
    )


def _candidate_capabilities_available(
    candidate: WorkloadSource,
    availability: Mapping[str, CapabilityAvailability],
    *,
    namespace: str | None,
) -> bool:
    controller_capability = _CONTROLLER_CAPABILITIES.get(candidate.controller_kind)
    if controller_capability is None:
        return False
    if namespace is not None and candidate.controller_namespace != namespace:
        return False
    required = [controller_capability]
    if candidate.controller_kind in {ControllerKind.FLUX_KUSTOMIZATION, ControllerKind.FLUX_HELM_RELEASE}:
        source_ref = candidate.evidence.get("source_ref")
        if isinstance(source_ref, Mapping):
            source_kind = source_ref.get("kind")
            source_capability = _FLUX_SOURCE_CAPABILITIES.get(source_kind) if isinstance(source_kind, str) else None
            if source_capability is not None:
                required.append(source_capability)
            source_namespace = source_ref.get("namespace")
            if namespace is not None and isinstance(source_namespace, str) and source_namespace != namespace:
                return False
        chart_ref = candidate.evidence.get("chart_ref")
        if isinstance(chart_ref, Mapping):
            chart_kind = chart_ref.get("kind")
            chart_capability = _FLUX_SOURCE_CAPABILITIES.get(chart_kind) if isinstance(chart_kind, str) else None
            if chart_capability is not None:
                required.append(chart_capability)
            chart_namespace = chart_ref.get("namespace")
            if namespace is not None and isinstance(chart_namespace, str) and chart_namespace != namespace:
                return False
    return all(_capability_available(name, availability) for name in required)


def _all_capabilities_available(
    availability: Mapping[str, CapabilityAvailability],
    *,
    namespace: str | None,
) -> bool:
    return namespace is None and all(_capability_available(name, availability) for name in _DISCOVERY_CAPABILITIES)


def _capability_available(
    capability: str,
    availability: Mapping[str, CapabilityAvailability],
) -> bool:
    status = availability.get(capability)
    return status is not None and status.available


def _mark_candidate_stale(candidate: WorkloadSource) -> WorkloadSource:
    evidence = dict(candidate.evidence)
    evidence["stale_reason"] = "source-not-observed-in-refresh"
    return candidate.model_copy(
        update={
            "status": BindingStatus.STALE,
            "reason": candidate.reason or "source-not-observed-in-refresh",
            "evidence": evidence,
        }
    )


def _stale_binding_without_candidates(binding: WorkloadBinding) -> WorkloadBinding:
    evidence = dict(binding.evidence)
    evidence["stale_reason"] = "workload-not-observed-in-refresh"
    return replace(
        binding,
        status=BindingStatus.STALE,
        reason=binding.reason or "workload-not-observed-in-refresh",
        evidence=evidence,
    )


def _preserve_resolution_history(
    binding: WorkloadBinding,
    previous: WorkloadBinding,
) -> WorkloadBinding:
    if not previous.evidence:
        return binding
    evidence = dict(binding.evidence)
    historical = previous.evidence.get("historical_resolution_evidence")
    if not isinstance(historical, Mapping):
        historical = previous.evidence
    evidence["historical_resolution_evidence"] = dict(historical)
    return replace(binding, evidence=evidence)


def _binding_identity(binding: WorkloadBinding) -> WorkloadIdentity:
    return binding.cluster, binding.namespace, binding.workload_kind, binding.workload_name


def _validate_interval(interval_seconds: float) -> None:
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or not MIN_DISCOVERY_INTERVAL_SECONDS <= interval_seconds <= MAX_DISCOVERY_INTERVAL_SECONDS
    ):
        raise ValueError(
            "interval_seconds must be finite and between "
            f"{MIN_DISCOVERY_INTERVAL_SECONDS:g} and {MAX_DISCOVERY_INTERVAL_SECONDS:g}"
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
