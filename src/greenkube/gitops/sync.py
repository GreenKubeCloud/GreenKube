"""Bounded, read-only observation of GitOps controller sync state."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from greenkube.gitops.contracts import BindingStatus, ControllerKind, WorkloadSource

_FULL_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.IGNORECASE)
_SHA1_COMPONENT = re.compile(r"^.+@sha1:([0-9a-f]{40})\Z", re.IGNORECASE)
_SHA256_COMPONENT = re.compile(r"^.+@sha256:([0-9a-f]{64})\Z", re.IGNORECASE)
_EXPLICIT_SHA1 = re.compile(r"^sha1:([0-9a-f]{40})\Z", re.IGNORECASE)
_EXPLICIT_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})\Z", re.IGNORECASE)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_ARGO_APPLICATION = ("argoproj.io", "v1alpha1", "applications", "Application")
_FLUX_KUSTOMIZATION = (
    "kustomize.toolkit.fluxcd.io",
    "v1",
    "kustomizations",
    "Kustomization",
)
_FLUX_HELM_RELEASE = ("helm.toolkit.fluxcd.io", "v2", "helmreleases", "HelmRelease")
_CONTROLLER_RESOURCES = {
    ControllerKind.ARGOCD: _ARGO_APPLICATION,
    ControllerKind.FLUX_KUSTOMIZATION: _FLUX_KUSTOMIZATION,
    ControllerKind.FLUX_HELM_RELEASE: _FLUX_HELM_RELEASE,
}
_MAX_LIST_ITEMS = 2
_MAX_RESPONSE_SIZE_BYTES = 512 * 1024
_MAX_RESPONSE_NODES = 20_000
_API_TIMEOUT_SECONDS = 10
_FLUX_SOURCE_KINDS = frozenset({"Bucket", "GitRepository", "HelmRepository", "OCIRepository"})
_FAILED_ARGO_HEALTH = frozenset({"degraded", "missing"})
_FAILED_ARGO_PHASES = frozenset({"failed", "error"})
_ACTIVE_ARGO_PHASES = frozenset({"running", "terminating"})
_FAILED_FLUX_REASONS = frozenset({"stalled", "rejected", "invalid"})


class GitOpsSyncStatus(StrEnum):
    """Safe high-level state of one controller's observed sync."""

    SYNCED = "synced"
    PENDING = "pending"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    MISMATCHED = "mismatched"


class GitOpsSyncReason(StrEnum):
    """Stable reason codes that never include controller response contents."""

    SYNC_CONFIRMED = "sync_confirmed"
    RECONCILIATION_PENDING = "reconciliation_pending"
    CONTROLLER_FAILED = "controller_failed"
    CONTROLLER_NOT_FOUND = "controller_not_found"
    CONTROLLER_AMBIGUOUS = "controller_ambiguous"
    CONTROLLER_IDENTITY_MISMATCH = "controller_identity_mismatch"
    CONTROLLER_NOT_READY = "controller_not_ready"
    SOURCE_UNRESOLVED = "source_unresolved"
    SOURCE_MISMATCH = "source_mismatch"
    SOURCE_AMBIGUOUS = "source_ambiguous"
    UNSUPPORTED_CONTROLLER = "unsupported_controller"
    REVISION_UNAVAILABLE = "revision_unavailable"
    REVISION_MISMATCH = "revision_mismatch"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    RESPONSE_TRUNCATED = "response_truncated"


@dataclass(frozen=True, slots=True)
class GitOpsSyncResult:
    """Typed, payload-free result for a single GitOps sync observation."""

    status: GitOpsSyncStatus
    reason: GitOpsSyncReason


async def observe_gitops_sync(
    source: WorkloadSource,
    merge_commit_sha: str,
    custom_objects_api: Any,
) -> GitOpsSyncResult:
    """Observe whether the source's exact GitOps controller applied ``merge_commit_sha``.

    The observer makes bounded namespaced custom-object list requests, selects
    only persisted controller/source names, caps each response at two objects
    and 512 KiB, and never returns resource or API error payloads. Flux
    HelmRelease sync is verified against its GitRepository artifact revision,
    not the chart version in ``lastAttemptedRevision``. HTTP 404 is unavailable;
    permission, timeout, and other API failures propagate so callers can retry.
    """
    if not isinstance(source, WorkloadSource):
        raise TypeError("source must be a WorkloadSource")
    if not isinstance(merge_commit_sha, str) or _FULL_COMMIT_SHA.fullmatch(merge_commit_sha) is None:
        raise ValueError("merge_commit_sha must be a full commit SHA")
    if custom_objects_api is None:
        raise ValueError("custom_objects_api must be provided")
    _validate_controller_identity(source)

    if source.status is not BindingStatus.RESOLVED:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.SOURCE_UNRESOLVED)

    resource = _CONTROLLER_RESOURCES.get(source.controller_kind)
    if resource is None:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.UNSUPPORTED_CONTROLLER)

    group, version, plural, expected_kind = resource
    try:
        async with asyncio.timeout(_API_TIMEOUT_SECONDS):
            response = await custom_objects_api.list_namespaced_custom_object(
                group=group,
                version=version,
                namespace=source.controller_namespace,
                plural=plural,
                field_selector=f"metadata.name={source.controller_name}",
                limit=_MAX_LIST_ITEMS,
                _request_timeout=_API_TIMEOUT_SECONDS,
            )
    except Exception as error:
        if _http_status(error) == 404:
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_FOUND)
        raise

    controller = _locate_controller(response, source)
    if isinstance(controller, GitOpsSyncResult):
        return controller
    if controller.get("kind") != expected_kind or controller.get("apiVersion") != f"{group}/{version}":
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    if source.controller_kind is ControllerKind.ARGOCD:
        return _observe_argo(controller, source, merge_commit_sha.lower())
    if source.controller_kind is ControllerKind.FLUX_HELM_RELEASE:
        return await _observe_flux_helm_release(controller, source, merge_commit_sha.lower(), custom_objects_api)
    return _observe_flux(controller, source, merge_commit_sha.lower())


def _validate_controller_identity(source: WorkloadSource) -> None:
    name = source.controller_name
    namespace = source.controller_namespace
    if not isinstance(name, str) or not _valid_dns_subdomain(name):
        raise ValueError("source has an invalid controller name")
    if not isinstance(namespace, str) or not _valid_dns_label(namespace):
        raise ValueError("source has an invalid controller namespace")


def _valid_dns_label(value: str) -> bool:
    return len(value) <= 63 and _DNS_LABEL.fullmatch(value) is not None


def _valid_dns_subdomain(value: str) -> bool:
    return len(value) <= 253 and all(_valid_dns_label(part) for part in value.split("."))


def _http_status(error: Exception) -> int | None:
    status = getattr(error, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _locate_controller(response: Any, source: WorkloadSource) -> Mapping[str, Any] | GitOpsSyncResult:
    payload_problem = _response_problem(response)
    if payload_problem is not None:
        return _result(GitOpsSyncStatus.UNAVAILABLE, payload_problem)
    if not isinstance(response, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    raw_items = response.get("items")
    metadata = response.get("metadata")
    if not isinstance(raw_items, list) or not isinstance(metadata, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    if len(raw_items) > _MAX_LIST_ITEMS:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.RESPONSE_TOO_LARGE)

    continuation = metadata.get("_continue")
    if continuation in (None, ""):
        continuation = metadata.get("continue")
    if continuation not in (None, ""):
        if not isinstance(continuation, str):
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.RESPONSE_TRUNCATED)

    matches: list[Mapping[str, Any]] = []
    has_unexpected_item = False
    for item in raw_items:
        if not isinstance(item, Mapping):
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
        item_metadata = item.get("metadata")
        if not isinstance(item_metadata, Mapping):
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
        name = item_metadata.get("name")
        namespace = item_metadata.get("namespace")
        if not isinstance(name, str) or not isinstance(namespace, str):
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
        if name == source.controller_name and namespace == source.controller_namespace:
            matches.append(item)
        else:
            has_unexpected_item = True

    if len(matches) > 1:
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.CONTROLLER_AMBIGUOUS)
    if has_unexpected_item:
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.CONTROLLER_IDENTITY_MISMATCH)
    if not matches:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_FOUND)

    return matches[0]


def _response_problem(response: Any) -> GitOpsSyncReason | None:
    if not isinstance(response, Mapping):
        return GitOpsSyncReason.INVALID_RESPONSE
    stack: list[Any] = [response]
    seen_containers: set[int] = set()
    node_count = 0
    estimated_bytes = 0

    while stack:
        value = stack.pop()
        node_count += 1
        if node_count > _MAX_RESPONSE_NODES:
            return GitOpsSyncReason.RESPONSE_TOO_LARGE
        estimated_bytes += 8
        if estimated_bytes > _MAX_RESPONSE_SIZE_BYTES:
            return GitOpsSyncReason.RESPONSE_TOO_LARGE

        if isinstance(value, Mapping):
            if id(value) in seen_containers:
                return GitOpsSyncReason.INVALID_RESPONSE
            seen_containers.add(id(value))
            if len(value) > _MAX_RESPONSE_NODES:
                return GitOpsSyncReason.RESPONSE_TOO_LARGE
            for key, child in value.items():
                if not isinstance(key, str):
                    return GitOpsSyncReason.INVALID_RESPONSE
                estimated_bytes += len(key) * 4
                if estimated_bytes > _MAX_RESPONSE_SIZE_BYTES:
                    return GitOpsSyncReason.RESPONSE_TOO_LARGE
                stack.append(child)
        elif isinstance(value, (list, tuple)):
            if id(value) in seen_containers:
                return GitOpsSyncReason.INVALID_RESPONSE
            seen_containers.add(id(value))
            if len(value) > _MAX_RESPONSE_NODES:
                return GitOpsSyncReason.RESPONSE_TOO_LARGE
            stack.extend(value)
        elif isinstance(value, str):
            estimated_bytes += len(value) * 4
            if estimated_bytes > _MAX_RESPONSE_SIZE_BYTES:
                return GitOpsSyncReason.RESPONSE_TOO_LARGE
        elif value is None or isinstance(value, (bool, float)):
            continue
        elif isinstance(value, int):
            estimated_bytes += max(1, (value.bit_length() + 7) // 8)
            if estimated_bytes > _MAX_RESPONSE_SIZE_BYTES:
                return GitOpsSyncReason.RESPONSE_TOO_LARGE
        else:
            return GitOpsSyncReason.INVALID_RESPONSE

    return None


def _observe_argo(
    application: Mapping[str, Any],
    source: WorkloadSource,
    merge_commit_sha: str,
) -> GitOpsSyncResult:
    spec = application.get("spec")
    status = application.get("status")
    if not isinstance(spec, Mapping) or not isinstance(status, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    sync = status.get("sync")
    health = status.get("health")
    operation_state = status.get("operationState")
    if not isinstance(sync, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_READY)
    if health is not None and not isinstance(health, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    if operation_state is not None and not isinstance(operation_state, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    operation_phase = _normalized_text(operation_state.get("phase")) if isinstance(operation_state, Mapping) else None
    health_status = _normalized_text(health.get("status")) if isinstance(health, Mapping) else None
    sync_status = _normalized_text(sync.get("status"))
    source_index, source_count, source_problem = _select_argo_source(source, spec)
    if source_problem is not None:
        return source_problem
    if source_index is None:
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)

    revision = _argo_revision(sync, source_index, source_count)
    if operation_phase in _FAILED_ARGO_PHASES or health_status in _FAILED_ARGO_HEALTH:
        return _result(GitOpsSyncStatus.FAILED, GitOpsSyncReason.CONTROLLER_FAILED)
    if operation_phase in _ACTIVE_ARGO_PHASES:
        return _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)

    if sync_status == "synced" and health_status == "healthy":
        return _compare_revision(revision, merge_commit_sha)
    if sync_status in {"outofsync", "unknown"} or health_status in {"progressing", "suspended", "unknown"}:
        return _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)
    if sync_status is None or health_status is None:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_READY)
    return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_READY)


def _select_argo_source(
    source: WorkloadSource,
    spec: Mapping[str, Any],
) -> tuple[int | None, int, GitOpsSyncResult | None]:
    has_single = spec.get("source") is not None
    has_multiple = spec.get("sources") is not None
    if has_single == has_multiple:
        return None, 0, _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)

    if has_single:
        configured = spec.get("source")
        if not isinstance(configured, Mapping) or not _argo_source_matches(source, configured):
            return None, 1, _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)
        return 0, 1, None

    configured_sources = spec.get("sources")
    if not isinstance(configured_sources, list) or not configured_sources:
        return None, 0, _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)
    if any(not isinstance(configured, Mapping) for configured in configured_sources):
        return None, len(configured_sources), _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    evidence = source.evidence
    if "source_index" in evidence:
        source_index = evidence.get("source_index")
        if isinstance(source_index, bool) or not isinstance(source_index, int):
            return (
                None,
                len(configured_sources),
                _result(
                    GitOpsSyncStatus.MISMATCHED,
                    GitOpsSyncReason.SOURCE_MISMATCH,
                ),
            )
        if source_index < 0 or source_index >= len(configured_sources):
            return (
                None,
                len(configured_sources),
                _result(
                    GitOpsSyncStatus.MISMATCHED,
                    GitOpsSyncReason.SOURCE_MISMATCH,
                ),
            )
        if not _argo_source_matches(source, configured_sources[source_index]):
            return (
                None,
                len(configured_sources),
                _result(
                    GitOpsSyncStatus.MISMATCHED,
                    GitOpsSyncReason.SOURCE_MISMATCH,
                ),
            )
        return source_index, len(configured_sources), None

    matching_indexes = [
        index for index, configured in enumerate(configured_sources) if _argo_source_matches(source, configured)
    ]
    if len(matching_indexes) > 1:
        return (
            None,
            len(configured_sources),
            _result(
                GitOpsSyncStatus.MISMATCHED,
                GitOpsSyncReason.SOURCE_AMBIGUOUS,
            ),
        )
    if not matching_indexes:
        return (
            None,
            len(configured_sources),
            _result(
                GitOpsSyncStatus.MISMATCHED,
                GitOpsSyncReason.SOURCE_MISMATCH,
            ),
        )
    return matching_indexes[0], len(configured_sources), None


def _argo_source_matches(source: WorkloadSource, configured: Mapping[str, Any]) -> bool:
    repository = configured.get("repoURL")
    target_revision = configured.get("targetRevision")
    if not isinstance(repository, str) or repository != source.repo_url:
        return False
    if target_revision is None or target_revision == "":
        target_revision = "HEAD"
    return isinstance(target_revision, str) and target_revision == source.ref


def _argo_revision(sync: Mapping[str, Any], source_index: int, source_count: int) -> str | None:
    if source_count > 1:
        revisions = sync.get("revisions")
        if not isinstance(revisions, list) or len(revisions) != source_count:
            return None
        revision = revisions[source_index]
    else:
        revisions = sync.get("revisions")
        if revisions is not None:
            if not isinstance(revisions, list) or len(revisions) != 1:
                return None
            revision = revisions[0]
        else:
            revision = sync.get("revision")
    return revision if isinstance(revision, str) and revision else None


def _observe_flux(
    controller: Mapping[str, Any],
    source: WorkloadSource,
    merge_commit_sha: str,
) -> GitOpsSyncResult:
    metadata = controller.get("metadata")
    status = controller.get("status")
    if not isinstance(metadata, Mapping) or not isinstance(status, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    source_problem = _flux_source_mismatch(controller, metadata, source)
    if source_problem is not None:
        return source_problem

    ready, readiness_result = _flux_readiness(metadata, status)
    if readiness_result is not None:
        return readiness_result
    if not ready:
        return _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)

    revision = _flux_revision(status, source.controller_kind)
    if not isinstance(revision, str) or not revision:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.REVISION_UNAVAILABLE)
    return _compare_revision(revision, merge_commit_sha)


async def _observe_flux_helm_release(
    controller: Mapping[str, Any],
    source: WorkloadSource,
    merge_commit_sha: str,
    custom_objects_api: Any,
) -> GitOpsSyncResult:
    metadata = controller.get("metadata")
    status = controller.get("status")
    spec = controller.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(status, Mapping) or not isinstance(spec, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    source_problem = _flux_source_mismatch(controller, metadata, source)
    if source_problem is not None:
        return source_problem
    ready, readiness_result = _flux_readiness(metadata, status)
    if readiness_result is not None:
        return readiness_result
    if not ready:
        return _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)

    chart = spec.get("chart")
    chart_spec = chart.get("spec") if isinstance(chart, Mapping) else None
    source_ref = chart_spec.get("sourceRef") if isinstance(chart_spec, Mapping) else None
    if not isinstance(source_ref, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.SOURCE_UNRESOLVED)
    if (_canonical_text(source_ref.get("kind")) or "HelmRepository") != "GitRepository":
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.REVISION_UNAVAILABLE)

    source_name = _canonical_text(source_ref.get("name"))
    source_namespace = _canonical_text(source_ref.get("namespace")) or _canonical_text(metadata.get("namespace"))
    if (
        source_name is None
        or not _valid_dns_subdomain(source_name)
        or source_namespace is None
        or not _valid_dns_label(source_namespace)
    ):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.SOURCE_UNRESOLVED)

    source_identity = source.model_copy(
        update={
            "controller_name": source_name,
            "controller_namespace": source_namespace,
        }
    )
    try:
        async with asyncio.timeout(_API_TIMEOUT_SECONDS):
            response = await custom_objects_api.list_namespaced_custom_object(
                group="source.toolkit.fluxcd.io",
                version="v1",
                namespace=source_namespace,
                plural="gitrepositories",
                field_selector=f"metadata.name={source_name}",
                limit=_MAX_LIST_ITEMS,
                _request_timeout=_API_TIMEOUT_SECONDS,
            )
    except Exception as error:
        if _http_status(error) == 404:
            return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_FOUND)
        raise

    git_repository = _locate_controller(response, source_identity)
    if isinstance(git_repository, GitOpsSyncResult):
        return git_repository
    if (
        git_repository.get("kind") != "GitRepository"
        or git_repository.get("apiVersion") != "source.toolkit.fluxcd.io/v1"
    ):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    git_metadata = git_repository.get("metadata")
    git_status = git_repository.get("status")
    if not isinstance(git_metadata, Mapping) or not isinstance(git_status, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    ready, readiness_result = _flux_readiness(git_metadata, git_status)
    if readiness_result is not None:
        return readiness_result
    if not ready:
        return _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)

    artifact = git_status.get("artifact")
    revision = artifact.get("revision") if isinstance(artifact, Mapping) else None
    if not isinstance(revision, str) or not revision:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.REVISION_UNAVAILABLE)
    return _compare_revision(revision, merge_commit_sha)


def _flux_source_mismatch(
    controller: Mapping[str, Any],
    metadata: Mapping[str, Any],
    source: WorkloadSource,
) -> GitOpsSyncResult | None:
    expected_ref = source.evidence.get("source_ref")
    spec = controller.get("spec")
    if not isinstance(expected_ref, Mapping) or not isinstance(spec, Mapping):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.SOURCE_UNRESOLVED)

    if source.controller_kind is ControllerKind.FLUX_KUSTOMIZATION:
        configured_ref = spec.get("sourceRef")
        default_kind = "GitRepository"
    else:
        chart = spec.get("chart")
        chart_spec = chart.get("spec") if isinstance(chart, Mapping) else None
        configured_ref = chart_spec.get("sourceRef") if isinstance(chart_spec, Mapping) else None
        default_kind = "HelmRepository"
    if not isinstance(configured_ref, Mapping):
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)

    expected_kind = _canonical_text(expected_ref.get("kind"))
    expected_name = _canonical_text(expected_ref.get("name"))
    expected_namespace = _canonical_text(expected_ref.get("namespace"))
    actual_kind = _canonical_text(configured_ref.get("kind")) or default_kind
    actual_name = _canonical_text(configured_ref.get("name"))
    actual_namespace = _canonical_text(configured_ref.get("namespace")) or _canonical_text(metadata.get("namespace"))
    if (
        expected_kind not in _FLUX_SOURCE_KINDS
        or expected_name is None
        or not _valid_dns_subdomain(expected_name)
        or expected_namespace is None
        or not _valid_dns_label(expected_namespace)
    ):
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.SOURCE_UNRESOLVED)
    if actual_kind != expected_kind or actual_name != expected_name or actual_namespace != expected_namespace:
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.SOURCE_MISMATCH)
    return None


def _flux_revision(status: Mapping[str, Any], controller_kind: ControllerKind) -> str | None:
    revision = status.get("lastAppliedRevision")
    if isinstance(revision, str) and revision:
        return revision
    if controller_kind is ControllerKind.FLUX_HELM_RELEASE:
        attempted_revision = status.get("lastAttemptedRevision")
        if isinstance(attempted_revision, str) and attempted_revision:
            return attempted_revision
    return None


def _flux_readiness(
    metadata: Mapping[str, Any],
    status: Mapping[str, Any],
) -> tuple[bool, GitOpsSyncResult | None]:
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_READY)

    ready_conditions = [
        condition for condition in conditions if isinstance(condition, Mapping) and condition.get("type") == "Ready"
    ]
    if len(ready_conditions) > 1:
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    for condition in conditions:
        if isinstance(condition, Mapping) and condition.get("type") == "Stalled":
            if _normalized_text(condition.get("status")) == "true":
                return False, _result(GitOpsSyncStatus.FAILED, GitOpsSyncReason.CONTROLLER_FAILED)

    if not ready_conditions:
        return False, _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)

    ready_condition = ready_conditions[0]
    ready_status = _normalized_text(ready_condition.get("status"))
    if ready_status not in {"true", "false", "unknown"}:
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    generation = _integer(metadata.get("generation"))
    raw_observed_generation = status.get("observedGeneration")
    raw_condition_generation = ready_condition.get("observedGeneration")
    observed_generation = _integer(raw_observed_generation)
    condition_generation = _integer(raw_condition_generation)
    if generation is None or generation < 1:
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    if (raw_observed_generation is not None and observed_generation is None) or (
        raw_condition_generation is not None and condition_generation is None
    ):
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    if condition_generation is not None and condition_generation < 0:
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)
    if observed_generation is None:
        observed_generation = condition_generation
    if observed_generation is None or observed_generation < 0:
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.CONTROLLER_NOT_READY)
    if observed_generation < generation or (condition_generation is not None and condition_generation < generation):
        return False, _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)
    if observed_generation > generation or (condition_generation is not None and condition_generation > generation):
        return False, _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.INVALID_RESPONSE)

    if ready_status == "false":
        reason = _normalized_text(ready_condition.get("reason")) or ""
        if reason in _FAILED_FLUX_REASONS or reason.endswith(("failed", "failure", "error")):
            return False, _result(GitOpsSyncStatus.FAILED, GitOpsSyncReason.CONTROLLER_FAILED)
        return False, _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)
    if ready_status == "unknown":
        return False, _result(GitOpsSyncStatus.PENDING, GitOpsSyncReason.RECONCILIATION_PENDING)
    return True, None


def _compare_revision(revision: str | None, merge_commit_sha: str) -> GitOpsSyncResult:
    observed_sha = _revision_commit_sha(revision)
    if observed_sha is None:
        return _result(GitOpsSyncStatus.UNAVAILABLE, GitOpsSyncReason.REVISION_UNAVAILABLE)
    if observed_sha != merge_commit_sha:
        return _result(GitOpsSyncStatus.MISMATCHED, GitOpsSyncReason.REVISION_MISMATCH)
    return _result(GitOpsSyncStatus.SYNCED, GitOpsSyncReason.SYNC_CONFIRMED)


def _revision_commit_sha(revision: str | None) -> str | None:
    if not isinstance(revision, str) or not revision or len(revision) > 512 or revision != revision.strip():
        return None
    if _FULL_COMMIT_SHA.fullmatch(revision) is not None:
        return revision.lower()
    for pattern in (_SHA1_COMPONENT, _SHA256_COMPONENT, _EXPLICIT_SHA1, _EXPLICIT_SHA256):
        match = pattern.fullmatch(revision)
        if match is not None:
            return match.group(1).lower()
    return None


def _normalized_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value.casefold()


def _canonical_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _result(status: GitOpsSyncStatus, reason: GitOpsSyncReason) -> GitOpsSyncResult:
    return GitOpsSyncResult(status=status, reason=reason)
