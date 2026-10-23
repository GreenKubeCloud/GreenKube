"""Pure mapping from Flux Kustomizations and HelmReleases to source candidates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from greenkube.gitops.contracts import (
    BindingStatus,
    ControllerKind,
    RendererKind,
    ResourceTarget,
    WorkloadSource,
    canonical_repository_identity,
)
from greenkube.gitops.discovery.common import (
    SUPPORTED_WORKLOAD_KINDS,
    WORKLOAD_KINDS,
    as_mapping,
    controller_evidence,
    flux_release_name,
    immutable_revision,
    join_repo_path,
    positive_generation,
    safe_repo_path,
    text,
    workload_source,
)

_API_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_KIND_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_DNS_SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_SOURCE_KINDS = frozenset({"Bucket", "GitRepository", "HelmRepository", "OCIRepository"})
_VALUES_KINDS = frozenset({"ConfigMap", "Secret"})


@dataclass(frozen=True)
class _FluxSource:
    kind: str
    name: str
    namespace: str
    repo_url: str | None
    ref: str
    artifact_revision: str | None
    revision: str | None
    uid: str | None
    generation: int | None
    raw: Mapping[str, Any] | None


@dataclass(frozen=True)
class _SourceResolution:
    sources: list[_FluxSource]
    status: BindingStatus | None
    reason: str | None


@dataclass(frozen=True)
class _FluxWorkload:
    kind: str
    namespace: str
    name: str
    api_version: str


@dataclass(frozen=True)
class _FluxChartSource:
    source_ref: Mapping[str, Any]
    resolution: _SourceResolution
    chart_name: str | None
    chart_version: str | None
    values_files: tuple[str, ...]
    values_files_error: str | None
    ignore_missing_values_files: bool
    chart_ref: dict[str, Any] | None
    chart_ref_uid: str | None
    chart_ref_generation: int | None


def parse_flux_inventory_id(inventory_id: str, version: str | None = None) -> ResourceTarget:
    """Parse legacy and current Flux inventory IDs."""
    if not isinstance(inventory_id, str) or not inventory_id or inventory_id != inventory_id.strip():
        raise ValueError("inventory ID must be a non-empty canonical string")
    parts = inventory_id.split("_")
    if len(parts) == 5:
        group, resource_version, kind, namespace, name = parts
    elif len(parts) == 4:
        namespace, name, group, kind = parts
        resource_version = text(version) or ""
    else:
        raise ValueError("inventory ID must contain four or five underscore-delimited fields")
    if group and not _API_TOKEN_PATTERN.fullmatch(group):
        raise ValueError("inventory ID contains an invalid API group")
    if not _API_TOKEN_PATTERN.fullmatch(resource_version):
        raise ValueError("inventory ID contains an invalid API version")
    if not _KIND_PATTERN.fullmatch(kind):
        raise ValueError("inventory ID contains an invalid kind")
    if not _is_dns_label(namespace) or not _is_dns_subdomain(name):
        raise ValueError("inventory ID contains an invalid namespace or resource name")
    api_version = f"{group}/{resource_version}" if group else resource_version
    return ResourceTarget(api_version=api_version, kind=kind, namespace=namespace, name=name)


def _is_dns_label(value: str) -> bool:
    return len(value) <= 63 and _DNS_LABEL_PATTERN.fullmatch(value) is not None


def _is_dns_subdomain(value: str) -> bool:
    return (
        len(value) <= 253
        and all(_DNS_LABEL_PATTERN.fullmatch(label) is not None for label in value.split("."))
        and _DNS_SUBDOMAIN_PATTERN.fullmatch(value) is not None
    )


def discover_flux_kustomization(
    kustomization: Mapping[str, Any],
    sources: Iterable[Mapping[str, Any]],
    *,
    cluster: str,
    provider_id: str,
    plain_manifest_paths: Iterable[str] = (),
    observed_at: datetime | None = None,
) -> list[WorkloadSource]:
    """Map Flux inventory entries through a supplied, already-fetched source set.

    ``plain_manifest_paths`` contains caller-supplied paths already known to be
    plain manifests; the adapter does not inspect a repository to classify them.
    """
    metadata = as_mapping(kustomization.get("metadata")) or {}
    spec = as_mapping(kustomization.get("spec")) or {}
    status = as_mapping(kustomization.get("status")) or {}
    controller_name = text(metadata.get("name")) or "unknown-kustomization"
    controller_namespace = text(metadata.get("namespace")) or "default"
    controller_uid = text(metadata.get("uid"))
    controller_generation = positive_generation(metadata.get("generation"))
    source_ref = as_mapping(spec.get("sourceRef")) or {}
    source_resolution = _resolve_source(
        source_ref,
        list(sources),
        default_namespace=controller_namespace,
        default_kind="GitRepository",
    )
    path_present = spec.get("path") is not None
    raw_path = spec.get("path") if path_present else "./"
    normalized_path = safe_repo_path(raw_path, allow_root=True)
    path = normalized_path or "."
    known_plain_paths = {
        known_path
        for raw_known_path in plain_manifest_paths
        if (known_path := safe_repo_path(raw_known_path, allow_root=True)) is not None
    }
    inventory = as_mapping(status.get("inventory")) or {}
    entries = inventory.get("entries")
    if not isinstance(entries, list):
        return []
    controller_state = controller_evidence(ControllerKind.FLUX_KUSTOMIZATION, spec, status)

    renderer = RendererKind.RAW if path in known_plain_paths else RendererKind.KUSTOMIZE
    if renderer is RendererKind.RAW:
        renderer_config: dict[str, Any] = {"manifest_path": path}
    else:
        renderer_config = {
            "overlay_path": path,
            "kustomization_path": join_repo_path(path, "kustomization.yaml"),
        }
    renderer_config["source_ref"] = {
        "kind": text(source_ref.get("kind")) or "GitRepository",
        "name": text(source_ref.get("name")),
        "namespace": text(source_ref.get("namespace")) or controller_namespace,
    }
    target_namespace = text(spec.get("targetNamespace"))
    if target_namespace is not None:
        renderer_config["target_namespace"] = target_namespace

    candidates: list[WorkloadSource] = []
    seen_inventory_ids: set[str] = set()
    for entry in entries:
        entry_mapping = as_mapping(entry)
        if entry_mapping is None:
            continue
        inventory_id = text(entry_mapping.get("id"))
        if inventory_id is None or inventory_id in seen_inventory_ids:
            continue
        seen_inventory_ids.add(inventory_id)
        try:
            target = parse_flux_inventory_id(inventory_id, version=text(entry_mapping.get("v")))
        except ValueError:
            continue
        if target.kind not in WORKLOAD_KINDS:
            continue
        workload = _FluxWorkload(
            kind=target.kind,
            namespace=target.namespace,
            name=target.name,
            api_version=target.api_version,
        )
        source_options = source_resolution.sources or [
            _source_stub(
                kind=text(source_ref.get("kind")) or "GitRepository",
                name=text(source_ref.get("name")) or "unresolved",
                namespace=text(source_ref.get("namespace")) or controller_namespace,
            )
        ]
        for flux_source in source_options:
            candidate_status = source_resolution.status or BindingStatus.RESOLVED
            reason = source_resolution.reason
            if candidate_status is BindingStatus.RESOLVED and (
                text(metadata.get("name")) is None
                or text(metadata.get("namespace")) is None
                or text(cluster) is None
                or text(provider_id) is None
            ):
                candidate_status = BindingStatus.UNRESOLVED
                reason = "kustomization-context-unresolved"
            elif normalized_path is None:
                candidate_status = BindingStatus.BLOCKED
                reason = "unsafe-source-path"
            elif workload.kind not in SUPPORTED_WORKLOAD_KINDS and candidate_status is BindingStatus.RESOLVED:
                candidate_status = BindingStatus.BLOCKED
                reason = (
                    "owner-resolution-required"
                    if workload.kind in {"Pod", "ReplicaSet"}
                    else "unsupported-workload-kind"
                )
            elif flux_source.repo_url is None and candidate_status is BindingStatus.RESOLVED:
                candidate_status = BindingStatus.UNRESOLVED
                reason = "source-url-unresolved"
            elif text(provider_id) is None and candidate_status is BindingStatus.RESOLVED:
                candidate_status = BindingStatus.UNRESOLVED
                reason = "provider-unresolved"

            evidence = {
                "kustomization": {"name": controller_name, "namespace": controller_namespace},
                "source_ref": {
                    "kind": flux_source.kind,
                    "name": flux_source.name,
                    "namespace": flux_source.namespace,
                },
                "artifact_revision": flux_source.artifact_revision,
                "inventory_id": inventory_id,
                "resource": {
                    "api_version": workload.api_version,
                    "kind": workload.kind,
                    "namespace": workload.namespace,
                    "name": workload.name,
                },
            }
            evidence.update({key: value for key, value in controller_state.items() if value is not None})
            candidates.append(
                workload_source(
                    cluster=cluster,
                    workload_namespace=workload.namespace,
                    workload_kind=workload.kind,
                    workload_name=workload.name,
                    provider_id=provider_id,
                    repo_url=flux_source.repo_url or "unresolved",
                    ref=flux_source.ref,
                    revision=flux_source.revision,
                    path=path,
                    controller_kind=ControllerKind.FLUX_KUSTOMIZATION,
                    controller_name=controller_name,
                    controller_namespace=controller_namespace,
                    controller_uid=controller_uid,
                    controller_generation=controller_generation,
                    source_uid=flux_source.uid,
                    source_generation=flux_source.generation,
                    source_revision=flux_source.artifact_revision,
                    renderer=renderer,
                    renderer_config=deepcopy(renderer_config),
                    status=candidate_status,
                    evidence=evidence,
                    reason=reason,
                    observed_at=observed_at,
                    identity_salt=(
                        f"{flux_source.kind}:{flux_source.namespace}:{flux_source.name}:"
                        f"{canonical_repository_identity(flux_source.repo_url or 'unresolved')}:{flux_source.ref}"
                    ),
                )
            )
    return candidates


def discover_flux_helm_release_manifest_source(
    helm_release: Mapping[str, Any],
    kustomizations: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    provider_id: str,
) -> dict[str, str] | None:
    """Find the Git Kustomization inventory that owns a HelmRelease manifest."""
    release_metadata = as_mapping(helm_release.get("metadata")) or {}
    release_name = text(release_metadata.get("name"))
    release_namespace = text(release_metadata.get("namespace"))
    if release_name is None or release_namespace is None:
        return None
    location, _ = _discover_flux_git_owned_resource_source(
        "HelmRelease",
        release_namespace,
        release_name,
        kustomizations,
        sources,
        provider_id=provider_id,
    )
    return location


def discover_flux_helm_release_values_sources(
    helm_release: Mapping[str, Any],
    kustomizations: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    provider_id: str,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Map valuesFrom references to unique Git-owned Flux inventory locations."""
    metadata = as_mapping(helm_release.get("metadata")) or {}
    spec = as_mapping(helm_release.get("spec")) or {}
    release_namespace = text(metadata.get("namespace")) or "default"
    raw_values_from = spec.get("valuesFrom", [])
    if raw_values_from is None:
        return {}
    if not isinstance(raw_values_from, list):
        return {}

    kustomization_objects = list(kustomizations)
    source_objects = list(sources)
    locations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw_reference in raw_values_from:
        reference = as_mapping(raw_reference)
        if reference is None:
            continue
        kind = text(reference.get("kind"))
        name = text(reference.get("name"))
        namespace = text(reference.get("namespace")) or release_namespace
        if kind is None or kind not in _VALUES_KINDS or name is None:
            continue
        key = (kind, namespace, name)
        location, reason = _discover_flux_git_owned_resource_source(
            kind,
            namespace,
            name,
            kustomization_objects,
            source_objects,
            provider_id=provider_id,
        )
        if location is not None:
            locations[key] = location
        elif reason == "ambiguous-values-source-owner":
            locations[key] = {"reason": reason}
    return locations


def _discover_flux_git_owned_resource_source(
    kind: str,
    resource_namespace: str,
    resource_name: str,
    kustomizations: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    provider_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Resolve a resource inventory entry to exactly one immutable Git source location."""

    source_objects = list(sources)
    locations: dict[tuple[str, str, str, str, str, str], dict[str, str]] = {}
    for kustomization in kustomizations:
        metadata = as_mapping(kustomization.get("metadata")) or {}
        spec = as_mapping(kustomization.get("spec")) or {}
        status = as_mapping(kustomization.get("status")) or {}
        namespace = text(metadata.get("namespace")) or "default"
        source_ref = as_mapping(spec.get("sourceRef")) or {}
        if (text(source_ref.get("kind")) or "GitRepository") != "GitRepository":
            continue

        inventory = as_mapping(status.get("inventory")) or {}
        entries = inventory.get("entries")
        if not isinstance(entries, list):
            continue
        owns_release = False
        for entry in entries:
            entry_mapping = as_mapping(entry)
            inventory_id = text(entry_mapping.get("id")) if entry_mapping is not None else None
            if inventory_id is None:
                continue
            try:
                target = parse_flux_inventory_id(
                    inventory_id,
                    version=text(entry_mapping.get("v")) if entry_mapping is not None else None,
                )
            except ValueError:
                continue
            if target.kind == kind and target.namespace == resource_namespace and target.name == resource_name:
                owns_release = True
                break
        if not owns_release:
            continue

        resolution = _resolve_source(
            source_ref,
            source_objects,
            default_namespace=namespace,
            default_kind="GitRepository",
        )
        if resolution.status is not None or len(resolution.sources) != 1:
            continue
        source = resolution.sources[0]
        if source.kind != "GitRepository" or source.repo_url is None or source.revision is None:
            continue

        path = safe_repo_path(spec.get("path") if spec.get("path") is not None else "./", allow_root=True)
        if path is None:
            continue
        location = {
            "provider_id": text(provider_id) or "unresolved",
            "repo_url": source.repo_url,
            "ref": source.ref,
            "revision": source.revision,
            "path": path,
        }
        target_namespace = text(spec.get("targetNamespace"))
        if target_namespace is not None:
            location["target_namespace"] = target_namespace
        key = (
            location["provider_id"],
            canonical_repository_identity(location["repo_url"]),
            location["ref"],
            location["revision"],
            location["path"],
            location.get("target_namespace", ""),
        )
        locations[key] = location

    if len(locations) == 1:
        return next(iter(locations.values())), None
    if len(locations) > 1:
        return None, "ambiguous-values-source-owner"
    return None, "values-source-not-in-git"


def _resolve_flux_chart_source(
    spec: Mapping[str, Any],
    sources: list[Mapping[str, Any]],
    *,
    controller_namespace: str,
) -> _FluxChartSource:
    if "chartRef" not in spec:
        chart = as_mapping(spec.get("chart")) or {}
        chart_spec = as_mapping(chart.get("spec"))
        if chart_spec is None:
            stub = _source_stub(kind="HelmRepository", name="unresolved", namespace=controller_namespace)
            return _FluxChartSource(
                source_ref={},
                resolution=_SourceResolution([stub], BindingStatus.BLOCKED, "helmrelease-chart-spec-unresolved"),
                chart_name=None,
                chart_version=None,
                values_files=(),
                values_files_error=None,
                ignore_missing_values_files=False,
                chart_ref=None,
                chart_ref_uid=None,
                chart_ref_generation=None,
            )

        source_ref = as_mapping(chart_spec.get("sourceRef")) or {}
        chart_version = text(chart_spec.get("version"))
        resolution = _resolve_source(
            source_ref,
            sources,
            default_namespace=controller_namespace,
            default_kind="HelmRepository",
            fallback_ref=chart_version,
        )
        values_files, values_files_error, ignore_missing = _chart_values_files(chart_spec)
        return _FluxChartSource(
            source_ref=source_ref,
            resolution=resolution,
            chart_name=text(chart_spec.get("chart")),
            chart_version=chart_version,
            values_files=values_files,
            values_files_error=values_files_error,
            ignore_missing_values_files=ignore_missing,
            chart_ref=None,
            chart_ref_uid=None,
            chart_ref_generation=None,
        )

    chart_ref_object = as_mapping(spec.get("chartRef"))
    if chart_ref_object is None:
        stub = _source_stub(kind="HelmChart", name="unresolved", namespace=controller_namespace)
        return _FluxChartSource(
            source_ref={},
            resolution=_SourceResolution([stub], BindingStatus.BLOCKED, "unsupported-chart-ref"),
            chart_name=None,
            chart_version=None,
            values_files=(),
            values_files_error=None,
            ignore_missing_values_files=False,
            chart_ref={},
            chart_ref_uid=None,
            chart_ref_generation=None,
        )

    chart_ref_kind = text(chart_ref_object.get("kind")) or "HelmChart"
    chart_ref_name = text(chart_ref_object.get("name"))
    chart_ref_namespace = text(chart_ref_object.get("namespace")) or controller_namespace
    chart_ref: dict[str, Any] = {"kind": chart_ref_kind}
    if chart_ref_name is not None:
        chart_ref["name"] = chart_ref_name
    chart_ref["namespace"] = chart_ref_namespace

    if chart_ref_kind == "HelmChart":
        if chart_ref_name is None:
            stub = _source_stub(kind="HelmChart", name="unresolved", namespace=chart_ref_namespace)
            resolution = _SourceResolution([stub], BindingStatus.UNRESOLVED, "chart-ref-unresolved")
            helm_chart_spec = None
            chart_ref_uid = None
            chart_ref_generation = None
        else:
            matches = []
            for source in sources:
                metadata = as_mapping(source.get("metadata")) or {}
                if (
                    text(source.get("kind")) == "HelmChart"
                    and text(metadata.get("name")) == chart_ref_name
                    and text(metadata.get("namespace")) == chart_ref_namespace
                ):
                    matches.append(source)
            if not matches:
                stub = _source_stub(kind="HelmChart", name=chart_ref_name, namespace=chart_ref_namespace)
                resolution = _SourceResolution([stub], BindingStatus.UNRESOLVED, "chart-ref-unresolved")
                helm_chart_spec = None
                chart_ref_uid = None
                chart_ref_generation = None
            elif len(matches) > 1:
                stub = _source_stub(kind="HelmChart", name=chart_ref_name, namespace=chart_ref_namespace)
                resolution = _SourceResolution([stub], BindingStatus.AMBIGUOUS, "chart-ref-ambiguous")
                helm_chart_spec = None
                chart_ref_uid = None
                chart_ref_generation = None
            else:
                helm_chart = matches[0]
                helm_chart_metadata = as_mapping(helm_chart.get("metadata")) or {}
                helm_chart_spec = as_mapping(helm_chart.get("spec"))
                chart_ref_uid = text(helm_chart_metadata.get("uid"))
                chart_ref_generation = positive_generation(helm_chart_metadata.get("generation"))
                if helm_chart_spec is None:
                    stub = _source_stub(kind="HelmChart", name=chart_ref_name, namespace=chart_ref_namespace)
                    resolution = _SourceResolution(
                        [stub],
                        BindingStatus.BLOCKED,
                        "chart-ref-spec-unresolved",
                    )
                else:
                    source_ref = as_mapping(helm_chart_spec.get("sourceRef")) or {}
                    chart_version = text(helm_chart_spec.get("version"))
                    resolution = _resolve_source(
                        source_ref,
                        sources,
                        default_namespace=chart_ref_namespace,
                        default_kind="HelmRepository",
                        fallback_ref=chart_version,
                    )
        if helm_chart_spec is None:
            source_ref = {"kind": "HelmChart", "name": chart_ref_name, "namespace": chart_ref_namespace}
            chart_name = None
            chart_version = None
            values_files = ()
            values_files_error = None
            ignore_missing = False
        else:
            source_ref = as_mapping(helm_chart_spec.get("sourceRef")) or {}
            chart_name = text(helm_chart_spec.get("chart"))
            chart_version = text(helm_chart_spec.get("version"))
            values_files, values_files_error, ignore_missing = _chart_values_files(helm_chart_spec)
    elif chart_ref_kind == "OCIRepository":
        source_ref = chart_ref_object
        resolution = _resolve_source(
            source_ref,
            sources,
            default_namespace=controller_namespace,
            default_kind="OCIRepository",
        )
        chart_name = None
        chart_version = None
        values_files = ()
        values_files_error = None
        ignore_missing = False
        matched = resolution.sources[0] if len(resolution.sources) == 1 else None
        chart_ref_uid = matched.uid if matched is not None else None
        chart_ref_generation = matched.generation if matched is not None else None
    else:
        stub = _source_stub(
            kind=chart_ref_kind,
            name=chart_ref_name or "unresolved",
            namespace=chart_ref_namespace,
        )
        source_ref = chart_ref_object
        resolution = _SourceResolution([stub], BindingStatus.BLOCKED, "unsupported-chart-ref-kind")
        chart_name = None
        chart_version = None
        values_files = ()
        values_files_error = None
        ignore_missing = False
        chart_ref_uid = None
        chart_ref_generation = None

    if resolution.status is None and (text(source_ref.get("kind")) or "HelmRepository") == "GitRepository":
        chart_path = safe_repo_path(chart_name, allow_root=True)
        if chart_path is None:
            reason = "chart-path-unresolved" if chart_name is None else "unsafe-chart-path"
            resolution = _SourceResolution(resolution.sources, BindingStatus.BLOCKED, reason)

    return _FluxChartSource(
        source_ref=source_ref,
        resolution=resolution,
        chart_name=chart_name,
        chart_version=chart_version,
        values_files=values_files,
        values_files_error=values_files_error,
        ignore_missing_values_files=ignore_missing,
        chart_ref=chart_ref,
        chart_ref_uid=chart_ref_uid,
        chart_ref_generation=chart_ref_generation,
    )


def _chart_values_files(chart_spec: Mapping[str, Any]) -> tuple[tuple[str, ...], str | None, bool]:
    raw_values_files = chart_spec.get("valuesFiles")
    values_files_error: str | None = None
    if raw_values_files is None:
        values_files: tuple[str, ...] = ()
    elif not isinstance(raw_values_files, list):
        values_files = ()
        values_files_error = "unsupported-chart-values-files"
    else:
        normalized: list[str] = []
        values_files_error = None
        for raw_path in raw_values_files:
            path = safe_repo_path(raw_path, allow_root=False)
            if path is None:
                values_files_error = "unsafe-chart-values-file-path"
                break
            normalized.append(path)
        values_files = tuple(normalized)
    ignore_missing = chart_spec.get("ignoreMissingValuesFiles", False)
    if type(ignore_missing) is not bool:
        return values_files, "unsupported-chart-values-files", False
    return values_files, values_files_error, ignore_missing


def discover_flux_helm_release(
    helm_release: Mapping[str, Any],
    resources: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    cluster: str,
    provider_id: str,
    manifest_source: Mapping[str, Any] | None = None,
    git_managed_values: Mapping[tuple[str, str, str], Mapping[str, Any]] | None = None,
    observed_at: datetime | None = None,
) -> list[WorkloadSource]:
    """Map live Helm release identities and values ownership without I/O.

    ``manifest_source`` identifies the Git-owned HelmRelease manifest.
    ``git_managed_values`` maps ``(kind, namespace, name)`` valuesFrom refs to
    trusted Git source locations; refs absent from that map are runtime-only.
    """
    metadata = as_mapping(helm_release.get("metadata")) or {}
    spec = as_mapping(helm_release.get("spec")) or {}
    status = as_mapping(helm_release.get("status")) or {}
    controller_state = controller_evidence(ControllerKind.FLUX_HELM_RELEASE, spec, status)
    controller_name = text(metadata.get("name")) or "unknown-helmrelease"
    controller_namespace = text(metadata.get("namespace")) or "default"
    controller_uid = text(metadata.get("uid"))
    controller_generation = positive_generation(metadata.get("generation"))
    release_name = flux_release_name(controller_name, spec)
    release_namespace = text(spec.get("targetNamespace")) or controller_namespace
    source_objects = list(sources)
    chart_source = _resolve_flux_chart_source(
        spec,
        source_objects,
        controller_namespace=controller_namespace,
    )
    source_ref = chart_source.source_ref
    source_resolution = chart_source.resolution
    matched_workloads = [
        workload
        for resource in resources
        if (workload := _helm_owned_workload(resource, release_name, release_namespace)) is not None
    ]
    matched_workloads = list(
        {
            (workload.api_version, workload.kind, workload.namespace, workload.name): workload
            for workload in matched_workloads
        }.values()
    )
    if not matched_workloads:
        return []

    values, values_error = _inline_values(spec)
    values_from, values_from_error = _values_from(
        spec,
        controller_namespace,
        git_managed_values or {},
        text(provider_id) or "unresolved",
    )
    post_renderers, post_renderer_error = _post_renderers(spec)
    source_ref_namespace = text(source_ref.get("namespace")) or (
        source_resolution.sources[0].namespace if len(source_resolution.sources) == 1 else controller_namespace
    )
    config = {
        "chart": {
            "name": chart_source.chart_name,
            "version": chart_source.chart_version,
            "repo_url": (source_resolution.sources[0].repo_url if len(source_resolution.sources) == 1 else None),
            "source_kind": text(source_ref.get("kind")) or "HelmRepository",
        },
        "chart_source_ref": {
            "kind": text(source_ref.get("kind")) or "HelmRepository",
            "name": text(source_ref.get("name")),
            "namespace": source_ref_namespace,
        },
        "chart_values_files": list(chart_source.values_files),
        "chart_ignore_missing_values_files": chart_source.ignore_missing_values_files,
        "release_name": release_name,
        "release_namespace": release_namespace,
        "values": values,
        "values_from": values_from,
        "post_renderers": post_renderers,
    }
    if chart_source.chart_ref is not None:
        chart_ref_config = deepcopy(chart_source.chart_ref)
        if chart_source.chart_ref_uid is not None:
            chart_ref_config["uid"] = chart_source.chart_ref_uid
        if chart_source.chart_ref_generation is not None:
            chart_ref_config["generation"] = chart_source.chart_ref_generation
        config["chart_ref"] = chart_ref_config

    manifest_location, manifest_error = _source_location(manifest_source, text(provider_id) or "unresolved")
    if values_from_error is None and manifest_location is not None:
        values_from_error = _validate_values_from_snapshot_locations(values_from, manifest_location)
    owners = [manifest_location]
    candidates: list[WorkloadSource] = []
    source_options = source_resolution.sources or [
        _source_stub(
            kind=text(source_ref.get("kind")) or "HelmRepository",
            name=text(source_ref.get("name")) or "unresolved",
            namespace=text(source_ref.get("namespace")) or controller_namespace,
            fallback_ref=chart_source.chart_version,
        )
    ]
    chart_ref_present = chart_source.chart_ref is not None

    for owner_location in owners:
        for flux_source in source_options:
            for workload in matched_workloads:
                candidate_status = source_resolution.status or BindingStatus.RESOLVED
                reason = source_resolution.reason
                repo_url = flux_source.repo_url or "unresolved"
                ref = flux_source.ref
                path = "."
                candidate_provider = text(provider_id) or "unresolved"

                if owner_location is not None:
                    repo_url = owner_location["repo_url"]
                    ref = owner_location["ref"]
                    path = owner_location["path"]
                    candidate_provider = owner_location["provider_id"]
                elif source_resolution.status is None:
                    candidate_status = BindingStatus.BLOCKED
                    reason = manifest_error or "helmrelease-not-in-git"

                if manifest_source is not None and manifest_location is None:
                    candidate_status = BindingStatus.BLOCKED
                    reason = manifest_error or "helmrelease-not-in-git"
                elif not source_resolution.sources and source_resolution.status is not None:
                    candidate_status = source_resolution.status
                    reason = source_resolution.reason

                if values_error is not None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.BLOCKED
                    reason = values_error
                elif values_from_error is not None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.BLOCKED
                    reason = values_from_error
                elif post_renderer_error is not None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.BLOCKED
                    reason = post_renderer_error
                elif chart_source.values_files_error is not None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.BLOCKED
                    reason = chart_source.values_files_error
                elif (
                    candidate_status is BindingStatus.RESOLVED
                    and owner_location is not None
                    and (chart_source_error := _chart_source_snapshot_reason(flux_source, owner_location)) is not None
                ):
                    candidate_status = BindingStatus.BLOCKED
                    reason = chart_source_error
                elif workload.kind not in SUPPORTED_WORKLOAD_KINDS and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.BLOCKED
                    reason = (
                        "owner-resolution-required"
                        if workload.kind in {"Pod", "ReplicaSet"}
                        else "unsupported-workload-kind"
                    )
                elif flux_source.repo_url is None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.UNRESOLVED
                    reason = "source-url-unresolved"
                elif text(provider_id) is None and candidate_status is BindingStatus.RESOLVED:
                    candidate_status = BindingStatus.UNRESOLVED
                    reason = "provider-unresolved"
                elif candidate_status is BindingStatus.RESOLVED and (
                    text(metadata.get("name")) is None
                    or text(metadata.get("namespace")) is None
                    or text(cluster) is None
                    or text(provider_id) is None
                ):
                    candidate_status = BindingStatus.UNRESOLVED
                    reason = "helmrelease-context-unresolved"
                elif path != "." and safe_repo_path(path, allow_root=True) is None:
                    candidate_status = BindingStatus.BLOCKED
                    reason = "unsafe-source-path"
                    path = "."

                candidate_config = deepcopy(config)
                if owner_location is not None:
                    manifest_source_config = {
                        "provider_id": owner_location["provider_id"],
                        "repo_url": owner_location["repo_url"],
                        "ref": owner_location["ref"],
                        "path": owner_location["path"],
                    }
                    if "revision" in owner_location:
                        manifest_source_config["revision"] = owner_location["revision"]
                    candidate_config["manifest_source"] = manifest_source_config
                evidence: dict[str, Any] = {
                    "helm_release": {"name": controller_name, "namespace": controller_namespace},
                    "release_identity": {
                        "name": release_name,
                        "namespace": release_namespace,
                    },
                    "source_ref": {
                        "kind": flux_source.kind,
                        "name": flux_source.name,
                        "namespace": flux_source.namespace,
                    },
                    "artifact_revision": flux_source.artifact_revision,
                    "resource": {
                        "api_version": workload.api_version,
                        "kind": workload.kind,
                        "namespace": workload.namespace,
                        "name": workload.name,
                    },
                }
                evidence.update({key: value for key, value in controller_state.items() if value is not None})
                if chart_ref_present:
                    chart_ref_evidence: dict[str, Any] = dict(chart_source.chart_ref or {})
                    if chart_source.chart_ref_uid is not None:
                        chart_ref_evidence["uid"] = chart_source.chart_ref_uid
                    if chart_source.chart_ref_generation is not None:
                        chart_ref_evidence["generation"] = chart_source.chart_ref_generation
                    evidence["chart_ref"] = chart_ref_evidence
                candidates.append(
                    workload_source(
                        cluster=cluster,
                        workload_namespace=workload.namespace,
                        workload_kind=workload.kind,
                        workload_name=workload.name,
                        provider_id=candidate_provider,
                        repo_url=repo_url,
                        ref=ref,
                        revision=immutable_revision(owner_location.get("revision")) if owner_location else None,
                        path=path,
                        controller_kind=ControllerKind.FLUX_HELM_RELEASE,
                        controller_name=controller_name,
                        controller_namespace=controller_namespace,
                        controller_uid=controller_uid,
                        controller_generation=controller_generation,
                        source_uid=flux_source.uid,
                        source_generation=flux_source.generation,
                        source_revision=flux_source.artifact_revision,
                        renderer=RendererKind.HELM_RELEASE,
                        renderer_config=candidate_config,
                        status=candidate_status,
                        evidence=evidence,
                        reason=reason,
                        observed_at=observed_at,
                        identity_salt=(
                            f"{flux_source.kind}:{flux_source.namespace}:{flux_source.name}:"
                            f"{canonical_repository_identity(flux_source.repo_url or 'unresolved')}:{flux_source.ref}"
                        ),
                    )
                )
    return candidates


def _resolve_source(
    source_ref: Mapping[str, Any],
    source_objects: list[Mapping[str, Any]],
    *,
    default_namespace: str,
    default_kind: str,
    fallback_ref: str | None = None,
) -> _SourceResolution:
    kind = text(source_ref.get("kind")) or default_kind
    name = text(source_ref.get("name"))
    namespace = text(source_ref.get("namespace")) or default_namespace
    if kind not in _SOURCE_KINDS:
        stub = _source_stub(kind=kind, name=name or "unresolved", namespace=namespace, fallback_ref=fallback_ref)
        return _SourceResolution([stub], BindingStatus.BLOCKED, "unsupported-source-kind")
    if name is None:
        stub = _source_stub(kind=kind, name="unresolved", namespace=namespace, fallback_ref=fallback_ref)
        return _SourceResolution([stub], BindingStatus.UNRESOLVED, "source-ref-unresolved")

    matches: list[Mapping[str, Any]] = []
    for source in source_objects:
        metadata = as_mapping(source.get("metadata")) or {}
        source_kind = text(source.get("kind"))
        if source_kind == kind and text(metadata.get("name")) == name and text(metadata.get("namespace")) == namespace:
            matches.append(source)
    if not matches:
        stub = _source_stub(kind=kind, name=name, namespace=namespace, fallback_ref=fallback_ref)
        return _SourceResolution([stub], BindingStatus.UNRESOLVED, "source-ref-unresolved")

    resolved = [_flux_source(source, kind=kind, fallback_ref=fallback_ref) for source in matches]
    if len(resolved) > 1:
        return _SourceResolution(resolved, BindingStatus.AMBIGUOUS, "source-ref-ambiguous")
    only_source = resolved[0]
    if only_source.repo_url is None:
        return _SourceResolution(resolved, BindingStatus.UNRESOLVED, "source-url-unresolved")
    if only_source.ref == "UNRESOLVED":
        return _SourceResolution(resolved, BindingStatus.BLOCKED, "unsupported-source-ref")
    return _SourceResolution(resolved, None, None)


def _flux_source(
    resource: Mapping[str, Any],
    *,
    kind: str,
    fallback_ref: str | None = None,
) -> _FluxSource:
    metadata = as_mapping(resource.get("metadata")) or {}
    spec = as_mapping(resource.get("spec")) or {}
    status = as_mapping(resource.get("status")) or {}
    artifact = as_mapping(status.get("artifact")) or {}
    name = text(metadata.get("name")) or "unresolved"
    namespace = text(metadata.get("namespace")) or "default"
    repo_url = _source_url(kind, spec)
    ref, ref_error = _configured_ref(kind, spec, fallback_ref)
    if ref_error is not None:
        ref = "UNRESOLVED"
    artifact_revision = text(artifact.get("revision"))
    return _FluxSource(
        kind=kind,
        name=name,
        namespace=namespace,
        repo_url=repo_url,
        ref=ref,
        artifact_revision=artifact_revision,
        revision=immutable_revision(artifact_revision) if kind == "GitRepository" else None,
        uid=text(metadata.get("uid")),
        generation=positive_generation(metadata.get("generation")),
        raw=resource,
    )


def _source_stub(
    *,
    kind: str,
    name: str,
    namespace: str,
    fallback_ref: str | None = None,
) -> _FluxSource:
    return _FluxSource(
        kind=kind,
        name=name,
        namespace=namespace,
        repo_url=None,
        ref=text(fallback_ref) or "HEAD",
        artifact_revision=None,
        revision=None,
        uid=None,
        generation=None,
        raw=None,
    )


def _chart_source_snapshot_reason(flux_source: _FluxSource, owner: Mapping[str, str]) -> str | None:
    if flux_source.kind != "GitRepository":
        return "remote-chart-not-in-snapshot"
    if flux_source.repo_url is None:
        return "chart-source-url-unresolved"
    if canonical_repository_identity(flux_source.repo_url) != canonical_repository_identity(owner["repo_url"]):
        return "chart-source-not-in-snapshot"
    source_revision = flux_source.revision
    owner_revision = immutable_revision(owner.get("revision"))
    if source_revision is None or owner_revision is None:
        return "chart-source-revision-unresolved"
    if source_revision.lower() != owner_revision.lower():
        return "chart-source-revision-mismatch"
    return None


def _validate_values_from_snapshot_locations(
    values_from: list[dict[str, Any]],
    manifest_location: Mapping[str, str],
) -> str | None:
    manifest_revision = immutable_revision(manifest_location.get("revision"))
    for value_source in values_from:
        if value_source.get("git_managed") is not True:
            continue
        repo_url = text(value_source.get("repo_url"))
        if repo_url is None or canonical_repository_identity(repo_url) != canonical_repository_identity(
            manifest_location["repo_url"]
        ):
            return "values-source-not-in-snapshot"
        if value_source.get("ref") != manifest_location.get("ref"):
            return "values-source-ref-mismatch"
        values_revision = immutable_revision(text(value_source.get("revision")))
        if manifest_revision is None or values_revision is None:
            return "values-source-revision-unresolved"
        if manifest_revision.lower() != values_revision.lower():
            return "values-source-revision-mismatch"
    return None


def _source_url(kind: str, spec: Mapping[str, Any]) -> str | None:
    if kind in {"GitRepository", "HelmRepository", "OCIRepository"}:
        return text(spec.get("url"))
    if kind == "Bucket":
        endpoint = text(spec.get("endpoint"))
        bucket_name = text(spec.get("bucketName"))
        if endpoint is None or bucket_name is None:
            return None
        if "://" in endpoint:
            base = endpoint.rstrip("/")
        else:
            scheme = "http" if spec.get("insecure") is True else "https"
            base = f"{scheme}://{endpoint.rstrip('/')}"
        return f"{base}/{bucket_name}"
    return None


def _configured_ref(kind: str, spec: Mapping[str, Any], fallback_ref: str | None) -> tuple[str, str | None]:
    ref = as_mapping(spec.get("ref")) or {}
    fields = (
        ("branch", "tag", "semver", "name", "commit")
        if kind == "GitRepository"
        else ("tag", "semver", "digest", "semverFilter")
        if kind == "OCIRepository"
        else ()
    )
    values = [text(ref.get(field)) for field in fields if text(ref.get(field)) is not None]
    if len(values) > 1:
        return "UNRESOLVED", "multiple-source-refs"
    if values:
        return values[0] or "HEAD", None
    return text(fallback_ref) or "HEAD", None


def _helm_owned_workload(
    resource: Mapping[str, Any],
    release_name: str,
    release_namespace: str,
) -> _FluxWorkload | None:
    metadata = as_mapping(resource.get("metadata")) or {}
    labels = as_mapping(metadata.get("labels")) or {}
    annotations = as_mapping(metadata.get("annotations")) or {}
    kind = text(resource.get("kind"))
    name = text(metadata.get("name"))
    namespace = text(metadata.get("namespace"))
    if kind is None or kind not in WORKLOAD_KINDS or name is None or namespace is None:
        return None
    if namespace != release_namespace:
        return None
    if labels.get("app.kubernetes.io/managed-by") != "Helm":
        return None
    if annotations.get("meta.helm.sh/release-name") != release_name:
        return None
    if annotations.get("meta.helm.sh/release-namespace") != release_namespace:
        return None
    return _FluxWorkload(
        kind=kind,
        namespace=namespace,
        name=name,
        api_version=text(resource.get("apiVersion")) or "unknown",
    )


def _inline_values(spec: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw_values = spec.get("values")
    if raw_values is None:
        return {}, None
    values = as_mapping(raw_values)
    if values is None:
        return {}, "unsupported-inline-values"
    return deepcopy(dict(values)), None


def _values_from(
    spec: Mapping[str, Any],
    helm_release_namespace: str,
    git_managed_values: Mapping[tuple[str, str, str], Mapping[str, Any]],
    default_provider_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    raw_values_from = spec.get("valuesFrom", [])
    if raw_values_from is None:
        raw_values_from = []
    if not isinstance(raw_values_from, list):
        return [], "unsupported-values-from"

    values_from: list[dict[str, Any]] = []
    error: str | None = None
    for value_ref in raw_values_from:
        reference = as_mapping(value_ref)
        if reference is None:
            return values_from, "unsupported-values-from"
        kind = text(reference.get("kind"))
        name = text(reference.get("name"))
        raw_namespace = reference.get("namespace")
        if raw_namespace is not None and (not isinstance(raw_namespace, str) or raw_namespace != raw_namespace.strip()):
            return values_from, "unsupported-values-from-namespace"
        namespace = text(raw_namespace) or helm_release_namespace
        if namespace != helm_release_namespace:
            return values_from, "unsupported-values-from-namespace"
        if kind is None or name is None:
            return values_from, "unsupported-values-source"
        raw_values_key = reference.get("valuesKey")
        if raw_values_key is not None and (
            not isinstance(raw_values_key, str)
            or not raw_values_key.strip()
            or raw_values_key != raw_values_key.strip()
        ):
            return values_from, "unsupported-values-from-key"
        raw_target_path = reference.get("targetPath")
        if raw_target_path not in (None, "") and not isinstance(raw_target_path, str):
            return values_from, "unsupported-values-from-target-path"
        if isinstance(raw_target_path, str) and raw_target_path.strip():
            return values_from, "unsupported-values-from-target-path"
        optional = reference.get("optional", False)
        if type(optional) is not bool:
            return values_from, "unsupported-values-from-optional"
        item: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "values_key": text(raw_values_key),
            "target_path": None,
            "optional": optional,
            "git_managed": False,
        }
        if kind not in _VALUES_KINDS:
            item["git_managed"] = False
            values_from.append(item)
            error = error or "unsupported-values-source"
            continue
        location_raw = git_managed_values.get((kind, namespace, name))
        if location_raw is None:
            values_from.append(item)
            error = error or "values-source-not-in-git"
            continue
        location_reason = text(location_raw.get("reason"))
        if location_reason is not None:
            values_from.append(item)
            error = error or location_reason
            continue
        location, location_error = _source_location(location_raw, default_provider_id)
        if location is None:
            values_from.append(item)
            error = error or location_error or "unsafe-values-source-path"
            continue
        item["git_managed"] = True
        item["provider_id"] = location["provider_id"]
        item["repo_url"] = location["repo_url"]
        item["ref"] = location["ref"]
        item["path"] = location["path"]
        if "revision" in location:
            item["revision"] = location["revision"]
        if "target_namespace" in location:
            item["target_namespace"] = location["target_namespace"]
        values_from.append(item)
    return values_from, error


def _post_renderers(spec: Mapping[str, Any]) -> tuple[list[Any], str | None]:
    raw_renderers = spec.get("postRenderers", [])
    if raw_renderers is None:
        raw_renderers = []
    if not isinstance(raw_renderers, list):
        return [], "unsupported-post-renderers"
    renderers: list[Any] = []
    for renderer in raw_renderers:
        if as_mapping(renderer) is None:
            return renderers, "unsupported-post-renderers"
        renderers.append(deepcopy(renderer))
    return renderers, None


def _source_location(
    location: Mapping[str, Any] | None,
    default_provider_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    if location is None:
        return None, None
    repo_url = text(location.get("repo_url")) or text(location.get("repoURL"))
    provider_id = text(location.get("provider_id")) or default_provider_id
    ref = (
        text(location.get("ref"))
        or text(location.get("target_revision"))
        or text(location.get("targetRevision"))
        or "HEAD"
    )
    revision = text(location.get("revision"))
    path = safe_repo_path(location.get("path"), allow_root=True)
    if path is None:
        return None, "unsafe-source-path"
    if repo_url is None or provider_id is None:
        return None, "source-location-unresolved"
    source_location = {"provider_id": provider_id, "repo_url": repo_url, "ref": ref, "path": path}
    if revision is not None:
        source_location["revision"] = revision
    target_namespace = text(location.get("target_namespace"))
    if target_namespace is not None:
        source_location["target_namespace"] = target_namespace
    return source_location, None
