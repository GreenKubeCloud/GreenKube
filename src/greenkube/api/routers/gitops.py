"""Read-only GitOps discovery, binding and coverage endpoints."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.api.dependencies import (
    get_authenticated_actor,
    get_gitops_binding_repository,
    get_gitops_cluster,
    get_gitops_discovery_runtime,
    get_gitops_discovery_source,
)
from greenkube.api.schemas import (
    GitOpsBindingProvenanceResponse,
    GitOpsCapabilityResponse,
    GitOpsCoverageResponse,
    GitOpsDiscoveryCapabilitiesResponse,
    GitOpsDiscoveryRefreshResponse,
    GitOpsDiscoveryStatusResponse,
    GitOpsErrorResponse,
    GitOpsManualBindingResolutionRequest,
    GitOpsWorkloadBindingListResponse,
    GitOpsWorkloadBindingResponse,
    GitOpsWorkloadSourceResponse,
)
from greenkube.gitops.bindings.manual import (
    ManualResolutionError,
    prepare_manual_binding_resolution,
)
from greenkube.gitops.contracts import (
    DEFAULT_BINDING_TTL,
    BindingStatus,
    WorkloadSource,
    binding_candidate_generation_token,
)
from greenkube.gitops.discovery.kubernetes import CapabilityAvailability, KubernetesDiscoverySource
from greenkube.gitops.discovery.runtime import GitOpsDiscoveryRuntime
from greenkube.gitops.discovery.service import DiscoveryCoverage
from greenkube.storage.base_gitops_repository import (
    WorkloadBinding,
    WorkloadBindingConflictError,
    WorkloadBindingCoverage,
    WorkloadBindingRepository,
    freshen_workload_binding,
    freshen_workload_source,
)

router = APIRouter(prefix="/gitops")

_MAX_BINDING_LIST_LIMIT = 100
_MAX_BINDING_LIST_CANDIDATES = 20
_MAX_BINDING_DETAIL_CANDIDATES = 50
_REVISION_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|sha1:[0-9a-fA-F]{40}|sha256:[0-9a-fA-F]{64})$")
_SCP_REPOSITORY_PATTERN = re.compile(r"^[^/@:\s]+@(?P<host>[^:/\s]+):(?P<path>[^?\s#]+)$")
# Expose only reason codes emitted by the GitOps adapters and reconciler.
_SAFE_REASON_CODES = frozenset(
    {
        "all-source-candidates-stale",
        "ambiguous-multi-source-owner",
        "ambiguous-values-source-owner",
        "application-context-unresolved",
        "application-not-in-git",
        "conflicting-chart-and-path",
        "conflicting-equal-priority-source-units",
        "conflicting-renderer-config",
        "conflicting-source-forms",
        "crd-not-found",
        "duplicate-candidate-id-conflict",
        "empty-sources",
        "helmrelease-context-unresolved",
        "helmrelease-not-in-git",
        "helm-values-not-resolvable",
        "helm-values-mapping-source-index-required",
        "highest-priority-source-blocked",
        "highest-priority-source-is-ambiguous",
        "highest-priority-source-unresolved",
        "invalid-helm-values-mapping",
        "invalid-source",
        "invalid-sources",
        "manual-resolution-expired",
        "manual-resolution-expiry-missing",
        "manual-resolution-source-changed",
        "kustomization-context-unresolved",
        "local-values-source-unresolved",
        "multiple-source-refs",
        "owner-resolution-required",
        "provider-unresolved",
        "source-expired",
        "source-location-unresolved",
        "source-not-observed-in-refresh",
        "source-observation-expired",
        "source-ref-ambiguous",
        "source-ref-unresolved",
        "source-repository-unresolved",
        "source-url-unresolved",
        "unsafe-application-source-path",
        "unsafe-source-path",
        "unsafe-values-file-path",
        "unsupported-config-management-plugin",
        "unsupported-directory-config",
        "unsupported-directory-renderer",
        "unsupported-helm-config",
        "unsupported-inline-values",
        "unsupported-kustomize-config",
        "unsupported-post-renderers",
        "unsupported-source-kind",
        "unsupported-source-ref",
        "unsupported-values-from",
        "unsupported-values-source",
        "unsupported-value-files",
        "unsupported-workload-kind",
        "unresolved-values-reference",
        "values-source-not-in-git",
        "workload-not-observed-in-refresh",
        "workload-source-unresolved",
    }
)
_CAPABILITY_NAMES = (
    "argocd",
    "flux_kustomization",
    "flux_helm_release",
    "flux_source_git_repository",
    "flux_source_oci_repository",
    "flux_source_helm_repository",
    "flux_source_bucket",
)


@router.get(
    "/status",
    response_model=GitOpsDiscoveryStatusResponse,
    responses={
        503: {
            "model": GitOpsErrorResponse,
            "description": "Kubernetes API configuration is unavailable.",
        },
        "default": {
            "model": GitOpsErrorResponse,
            "description": "Kubernetes RBAC, network, or server error.",
        },
    },
)
async def get_gitops_status(
    cluster: str = Depends(get_gitops_cluster),
    discovery_source: KubernetesDiscoverySource = Depends(get_gitops_discovery_source),
    repository: WorkloadBindingRepository = Depends(get_gitops_binding_repository),
) -> GitOpsDiscoveryStatusResponse:
    """Report bounded controller capability checks and persisted binding freshness."""
    try:
        availability = await discovery_source.check_availability()
    except ApiException as error:
        status_code = error.status
        if not isinstance(status_code, int) or not 400 <= status_code <= 599:
            status_code = 502
        raise HTTPException(status_code=status_code, detail="Kubernetes discovery request failed.") from error
    except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError) as error:
        raise HTTPException(status_code=503, detail="Kubernetes discovery is unavailable.") from error
    summary = await repository.get_workload_binding_coverage(cluster=cluster)
    return GitOpsDiscoveryStatusResponse(
        cluster=cluster,
        discovery=_discovery_capabilities_response(availability),
        last_refresh=_as_utc(summary.last_refresh) if summary.last_refresh is not None else None,
        coverage=_coverage_response(summary),
    )


@router.post(
    "/refresh",
    response_model=GitOpsDiscoveryRefreshResponse,
    responses={
        503: {
            "model": GitOpsErrorResponse,
            "description": "Kubernetes discovery is unavailable.",
        },
        "default": {
            "model": GitOpsErrorResponse,
            "description": "Kubernetes discovery refresh failed.",
        },
    },
)
async def refresh_gitops_discovery(
    cluster: str = Depends(get_gitops_cluster),
    runtime: GitOpsDiscoveryRuntime = Depends(get_gitops_discovery_runtime),
) -> GitOpsDiscoveryRefreshResponse:
    """Trigger one immediate discovery refresh and persist its binding coverage."""
    try:
        result = await runtime.refresh()
    except ApiException as error:
        status_code = error.status
        if not isinstance(status_code, int) or not 400 <= status_code <= 599:
            status_code = 502
        raise HTTPException(status_code=status_code, detail="Kubernetes discovery request failed.") from error
    except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError) as error:
        raise HTTPException(status_code=503, detail="Kubernetes discovery is unavailable.") from error
    return GitOpsDiscoveryRefreshResponse(
        cluster=cluster,
        refreshed_at=result.refreshed_at,
        coverage=_coverage_response(result.coverage),
    )


@router.get("/bindings", response_model=GitOpsWorkloadBindingListResponse)
async def list_gitops_workload_bindings(
    cluster: str = Depends(get_gitops_cluster),
    status: list[BindingStatus] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_MAX_BINDING_LIST_LIMIT),
    cursor: str | None = Query(default=None, max_length=2048),
    repository: WorkloadBindingRepository = Depends(get_gitops_binding_repository),
) -> GitOpsWorkloadBindingListResponse:
    """List a cursor-paginated page of safe workload binding summaries."""
    now = datetime.now(timezone.utc)
    try:
        page = await repository.list_workload_bindings_page(
            cluster=cluster,
            limit=limit,
            cursor=cursor,
            statuses=status,
            candidate_limit=_MAX_BINDING_LIST_CANDIDATES,
            now=now,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid GitOps binding cursor.") from error
    return GitOpsWorkloadBindingListResponse(
        items=[
            _workload_binding_response(
                binding,
                candidate_limit=_MAX_BINDING_LIST_CANDIDATES,
                now=now,
            )
            for binding in page.items
        ],
        limit=limit,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.get(
    "/bindings/{namespace}/{workload_kind}/{workload_name}",
    response_model=GitOpsWorkloadBindingResponse,
    responses={
        404: {
            "model": GitOpsErrorResponse,
            "description": "No GitOps binding exists for this workload.",
        }
    },
)
async def get_gitops_workload_binding(
    namespace: str,
    workload_kind: str,
    workload_name: str,
    cluster: str = Depends(get_gitops_cluster),
    repository: WorkloadBindingRepository = Depends(get_gitops_binding_repository),
) -> GitOpsWorkloadBindingResponse:
    """Return one workload's persisted binding and safe source provenance."""
    binding = await repository.get_workload_binding(cluster, namespace, workload_kind, workload_name)
    if binding is None:
        raise HTTPException(status_code=404, detail="GitOps workload binding not found.")
    return _workload_binding_response(binding)


@router.post(
    "/bindings/{namespace}/{workload_kind}/{workload_name}/resolve",
    response_model=GitOpsWorkloadBindingResponse,
    responses={
        404: {
            "model": GitOpsErrorResponse,
            "description": "No GitOps binding exists for this workload.",
        },
        409: {
            "model": GitOpsErrorResponse,
            "description": "The binding or selected source generation is no longer current.",
        },
    },
)
async def resolve_gitops_workload_binding(
    namespace: str,
    workload_kind: str,
    workload_name: str,
    request: GitOpsManualBindingResolutionRequest,
    http_request: Request,
    cluster: str = Depends(get_gitops_cluster),
    repository: WorkloadBindingRepository = Depends(get_gitops_binding_repository),
) -> GitOpsWorkloadBindingResponse:
    """Resolve an actionable binding using only a current persisted candidate."""
    binding = await repository.get_workload_binding(cluster, namespace, workload_kind, workload_name)
    if binding is None:
        raise HTTPException(status_code=404, detail="GitOps workload binding not found.")
    from greenkube.core.config import get_config

    binding_ttl = getattr(get_config(), "gitops_binding_ttl", DEFAULT_BINDING_TTL)
    try:
        prepared = prepare_manual_binding_resolution(
            binding,
            candidate_id=request.candidate_id,
            generation_token=request.generation_token,
            actor=get_authenticated_actor(http_request),
            ttl=binding_ttl,
            max_ttl=binding_ttl,
        )
        saved = await repository.save_manual_workload_binding(
            prepared.binding,
            prepared.event,
            expected_candidate_id=prepared.selected_candidate_id,
            expected_generation_token=prepared.generation_token,
            now=prepared.event.created_at,
        )
    except (ManualResolutionError, WorkloadBindingConflictError) as error:
        raise HTTPException(
            status_code=409,
            detail="GitOps binding changed; refresh candidates before resolving.",
        ) from error
    return _workload_binding_response(saved)


@router.get("/coverage", response_model=GitOpsCoverageResponse)
async def get_gitops_coverage(
    cluster: str = Depends(get_gitops_cluster),
    repository: WorkloadBindingRepository = Depends(get_gitops_binding_repository),
) -> GitOpsCoverageResponse:
    """Return freshness-aware workload binding counts for the selected cluster."""
    return _coverage_response(await repository.get_workload_binding_coverage(cluster=cluster))


def _discovery_capabilities_response(
    availability: Mapping[str, CapabilityAvailability],
) -> GitOpsDiscoveryCapabilitiesResponse:
    return GitOpsDiscoveryCapabilitiesResponse(
        **{capability: _capability_response(availability[capability]) for capability in _CAPABILITY_NAMES}
    )


def _capability_response(availability: CapabilityAvailability) -> GitOpsCapabilityResponse:
    return GitOpsCapabilityResponse(available=availability.available, reason=_public_reason(availability.reason))


def _coverage_response(coverage: DiscoveryCoverage | WorkloadBindingCoverage) -> GitOpsCoverageResponse:
    return GitOpsCoverageResponse(
        total=coverage.total,
        resolved=coverage.resolved,
        ambiguous=coverage.ambiguous,
        unresolved=coverage.unresolved,
        blocked=coverage.blocked,
        stale=coverage.stale,
        ratio=coverage.ratio,
    )


def _workload_binding_response(
    binding: WorkloadBinding,
    *,
    candidate_limit: int = _MAX_BINDING_DETAIL_CANDIDATES,
    now: datetime | None = None,
) -> GitOpsWorkloadBindingResponse:
    current_at = now or datetime.now(timezone.utc)
    binding = freshen_workload_binding(binding, now=current_at)
    candidates = sorted(binding.candidates, key=_source_sort_key)
    visible_candidates = candidates[:candidate_limit]
    return GitOpsWorkloadBindingResponse(
        cluster=_bounded_text(binding.cluster, 128),
        namespace=_bounded_text(binding.namespace, 128),
        workload_kind=_bounded_text(binding.workload_kind, 128),
        workload_name=_bounded_text(binding.workload_name, 128),
        status=binding.status,
        confidence=binding.confidence,
        binding=_source_response(binding.source, now=current_at) if binding.source is not None else None,
        candidates=[_source_response(candidate, now=current_at) for candidate in visible_candidates],
        candidates_truncated=binding.candidates_truncated or len(candidates) > candidate_limit,
        reason=_public_reason(binding.reason),
        provenance=[_provenance_response(candidate) for candidate in _provenance_sources(binding, visible_candidates)],
        observed_at=_as_utc(binding.observed_at),
        expires_at=_as_utc(binding.expires_at) if binding.expires_at is not None else None,
    )


def _source_response(source: WorkloadSource, *, now: datetime | None = None) -> GitOpsWorkloadSourceResponse:
    status, reason = _current_source_status(source, now=now)
    return GitOpsWorkloadSourceResponse(
        id=_bounded_text(source.id, 256),
        controller=f"{source.controller_kind.value}/{_bounded_text(source.controller_name, 128)}",
        controller_kind=source.controller_kind,
        controller_name=_bounded_text(source.controller_name, 128),
        controller_namespace=_bounded_text(source.controller_namespace, 128),
        provider_id=_bounded_text(source.provider_id, 128),
        repo_url=_safe_repository_url(source.repo_url),
        path=_bounded_text(source.path, 512),
        renderer=source.renderer,
        confidence=source.confidence,
        status=status,
        reason=_public_reason(reason),
        revision=_public_revision(source.revision),
        source_revision=_public_revision(source.source_revision),
        observed_at=_as_utc(source.observed_at),
        expires_at=_as_utc(source.expires_at) if source.expires_at is not None else None,
        generation_token=binding_candidate_generation_token(source),
    )


def _current_source_status(
    source: WorkloadSource,
    *,
    now: datetime | None = None,
) -> tuple[BindingStatus, str | None]:
    current = freshen_workload_source(source, now=now or datetime.now(timezone.utc))
    return current.status, current.reason


def _provenance_response(source: WorkloadSource) -> GitOpsBindingProvenanceResponse:
    return GitOpsBindingProvenanceResponse(
        source_id=_bounded_text(source.id, 256),
        controller=f"{source.controller_kind.value}/{_bounded_text(source.controller_name, 128)}",
        controller_kind=source.controller_kind,
        controller_name=_bounded_text(source.controller_name, 128),
        controller_namespace=_bounded_text(source.controller_namespace, 128),
        provider_id=_bounded_text(source.provider_id, 128),
        repo_url=_safe_repository_url(source.repo_url),
        path=_bounded_text(source.path, 512),
        renderer=source.renderer,
        revision=_public_revision(source.revision),
        source_revision=_public_revision(source.source_revision),
        observed_at=_as_utc(source.observed_at),
    )


def _provenance_sources(
    binding: WorkloadBinding,
    candidates: list[WorkloadSource],
) -> list[WorkloadSource]:
    by_id = {candidate.id: candidate for candidate in candidates}
    raw_provenance = binding.evidence.get("provenance")
    if isinstance(raw_provenance, list):
        source_ids = [
            item.get("source_id")
            for item in raw_provenance
            if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
        ]
        selected = [by_id[source_id] for source_id in source_ids if source_id in by_id]
        if selected:
            return sorted(selected, key=_source_sort_key)
    return candidates


def _source_sort_key(source: WorkloadSource) -> tuple[str, str, str, str, str, str, str]:
    return (
        source.controller_kind.value,
        source.controller_namespace,
        source.controller_name,
        source.provider_id,
        source.repo_url,
        source.path,
        source.id,
    )


def _safe_repository_url(value: str) -> str:
    if value == "unresolved":
        return value
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        return "redacted"
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "redacted"
    if parts.scheme.lower() in {"http", "https", "ssh", "git"} and host:
        netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))
    scp_match = _SCP_REPOSITORY_PATTERN.fullmatch(value)
    if scp_match is not None:
        return f"{scp_match.group('host')}:{scp_match.group('path')}"
    return "redacted"


def _bounded_text(value: str, limit: int) -> str:
    return value if len(value) <= limit and not any(ord(character) < 32 for character in value) else "redacted"


def _public_reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized in _SAFE_REASON_CODES else "redacted"


def _public_revision(value: str | None) -> str | None:
    if value is None or not _REVISION_PATTERN.fullmatch(value):
        return None
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
