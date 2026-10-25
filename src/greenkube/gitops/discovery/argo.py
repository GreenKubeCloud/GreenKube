"""Pure mapping from Argo CD Applications to workload source candidates."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from greenkube.gitops.contracts import BindingStatus, ControllerKind, RendererKind, WorkloadSource
from greenkube.gitops.discovery.common import (
    SUPPORTED_WORKLOAD_KINDS,
    WORKLOAD_KINDS,
    as_mapping,
    controller_evidence,
    immutable_revision,
    join_repo_path,
    positive_generation,
    safe_repo_path,
    text,
    workload_source,
)

_HELM_VALUES_MAPPING_ANNOTATION = "greenkube.cloud/helm-values-mapping"
_MAX_HELM_VALUES_MAPPING_BYTES = 4096


@dataclass(frozen=True)
class _ArgoSource:
    index: int
    raw: Mapping[str, Any]
    repo_url: str | None
    ref: str
    path: str
    path_valid: bool
    has_path: bool
    ref_name: str | None
    chart: str | None
    helm: Mapping[str, Any] | None
    helm_present: bool
    kustomize: Mapping[str, Any] | None
    kustomize_present: bool
    directory: Mapping[str, Any] | None


@dataclass(frozen=True)
class _ArgoWorkload:
    kind: str
    namespace: str
    name: str
    group: str | None
    version: str | None
    tracking_id: str | None = None


def discover_argo_application(
    application: Mapping[str, Any],
    *,
    cluster: str,
    provider_id: str,
    application_source: Mapping[str, Any] | None = None,
    kustomization_paths: Iterable[str] = (),
    live_resources: Iterable[Mapping[str, Any]] = (),
    observed_at: datetime | None = None,
) -> list[WorkloadSource]:
    """Map an Application's live resources and source configuration without I/O.

    ``application_source`` is optional, trusted Git ownership metadata for the
    Application manifest itself. It is required before inline Helm values can
    be treated as an editable source. ``kustomization_paths`` contains
    caller-supplied repository-relative paths already known to have a
    kustomization file; this adapter never probes repositories.
    """
    metadata = as_mapping(application.get("metadata")) or {}
    spec = as_mapping(application.get("spec")) or {}
    status = as_mapping(application.get("status")) or {}
    destination = as_mapping(spec.get("destination")) or {}
    controller_name = text(metadata.get("name")) or "unknown-application"
    controller_namespace = text(metadata.get("namespace")) or "default"
    controller_uid = text(metadata.get("uid"))
    controller_generation = positive_generation(metadata.get("generation"))
    destination_namespace = text(destination.get("namespace"))
    helm_values_mapping, helm_values_mapping_error = _helm_values_mapping(metadata)

    resources = _application_workloads(
        status,
        metadata,
        destination_namespace,
        controller_name,
        live_resources=live_resources,
    )
    if not resources:
        return []
    controller_state = controller_evidence(ControllerKind.ARGOCD, spec, status)

    sources, source_form_error = _application_sources(spec)
    detected_source_type = text(status.get("sourceType"))
    application_source_type = (
        detected_source_type.casefold() if len(sources) == 1 and detected_source_type is not None else None
    )
    known_kustomization_paths = {
        path for raw_path in kustomization_paths if (path := safe_repo_path(raw_path, allow_root=True)) is not None
    }
    revisions = _deployed_revisions(status, len(sources))
    owners = _owner_sources(sources)
    forced_status: BindingStatus | None = None
    forced_reason: str | None = None

    if source_form_error == "conflicting-source-forms":
        owners = sources
        forced_status = BindingStatus.AMBIGUOUS
        forced_reason = "conflicting-source-forms"
    elif source_form_error is not None:
        owners = []
        forced_status = BindingStatus.BLOCKED
        forced_reason = "workload-source-unresolved"
    elif len(owners) > 1:
        forced_status = BindingStatus.AMBIGUOUS
        forced_reason = "ambiguous-multi-source-owner"

    if forced_status is None and (
        text(metadata.get("name")) is None
        or text(metadata.get("namespace")) is None
        or text(cluster) is None
        or text(provider_id) is None
    ):
        forced_status = BindingStatus.UNRESOLVED
        forced_reason = "application-context-unresolved"

    if not owners:
        unresolved = _source_stub(-1)
        owners_to_map = [unresolved]
        if forced_status is None:
            forced_status = BindingStatus.UNRESOLVED
            forced_reason = "workload-source-unresolved"
    else:
        owners_to_map = owners

    candidates: list[WorkloadSource] = []
    for owner in owners_to_map:
        for resource in resources:
            candidates.append(
                _candidate_for_workload(
                    owner=owner,
                    sources=sources,
                    revisions=revisions,
                    resource=resource,
                    cluster=cluster,
                    provider_id=provider_id,
                    application_name=controller_name,
                    application_namespace=controller_namespace,
                    application_uid=controller_uid,
                    application_generation=controller_generation,
                    destination_namespace=destination_namespace,
                    helm_values_mapping=helm_values_mapping,
                    helm_values_mapping_error=helm_values_mapping_error,
                    application_source=application_source,
                    kustomization_paths=known_kustomization_paths,
                    application_source_type=application_source_type,
                    forced_status=forced_status,
                    forced_reason=forced_reason,
                    controller_state=controller_state,
                    observed_at=observed_at,
                )
            )
    return candidates


def _helm_values_mapping(
    metadata: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    annotations = as_mapping(metadata.get("annotations")) or {}
    raw_value = annotations.get(_HELM_VALUES_MAPPING_ANNOTATION)
    if raw_value is None:
        return None, None
    if not isinstance(raw_value, str):
        return None, "invalid-helm-values-mapping"
    try:
        if len(raw_value.encode("utf-8")) > _MAX_HELM_VALUES_MAPPING_BYTES:
            return None, "invalid-helm-values-mapping"
    except UnicodeEncodeError:
        return None, "invalid-helm-values-mapping"
    try:
        value = json.loads(raw_value, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeEncodeError, ValueError, RecursionError):
        return None, "invalid-helm-values-mapping"
    if not isinstance(value, dict) or set(value) - {"file_path", "container", "path", "source_index"}:
        return None, "invalid-helm-values-mapping"
    file_path = value.get("file_path")
    container = value.get("container")
    path = value.get("path")
    source_index = value.get("source_index")
    if (
        not isinstance(file_path, str)
        or safe_repo_path(file_path, allow_root=False) != file_path
        or not isinstance(container, str)
        or not container
        or container != container.strip()
        or not isinstance(path, list)
        or not path
        or len(path) > 32
        or any(not isinstance(part, str) or not part or part != part.strip() for part in path)
        or path[-1] != "resources"
        or (
            source_index is not None
            and (isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0)
        )
    ):
        return None, "invalid-helm-values-mapping"
    return {
        "file_path": file_path,
        "container": container,
        "path": path,
        **({"source_index": source_index} if source_index is not None else {}),
    }, None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _application_sources(spec: Mapping[str, Any]) -> tuple[list[_ArgoSource], str | None]:
    single_present = "source" in spec and spec.get("source") is not None
    multiple_present = "sources" in spec and spec.get("sources") is not None
    raw_sources: list[Mapping[str, Any]] = []
    error: str | None = None

    if single_present and multiple_present:
        error = "conflicting-source-forms"
        single = as_mapping(spec.get("source"))
        if single is not None:
            raw_sources.append(single)
        multiple = spec.get("sources")
        if isinstance(multiple, list):
            raw_sources.extend(item for item in multiple if isinstance(item, Mapping))
    elif single_present:
        single = as_mapping(spec.get("source"))
        if single is None:
            error = "invalid-source"
        else:
            raw_sources.append(single)
    elif multiple_present:
        multiple = spec.get("sources")
        if not isinstance(multiple, list):
            error = "invalid-sources"
        else:
            for item in multiple:
                if isinstance(item, Mapping):
                    raw_sources.append(item)
                else:
                    raw_sources.append({})
                    error = "invalid-sources"
            if not raw_sources:
                error = "empty-sources"
    else:
        error = "missing-source"

    return [_parse_source(index, source) for index, source in enumerate(raw_sources)], error


def _parse_source(index: int, source: Mapping[str, Any]) -> _ArgoSource:
    raw_path = source.get("path")
    has_path = "path" in source and raw_path is not None
    path = safe_repo_path(raw_path if has_path else ".", allow_root=True)
    helm_value = source.get("helm")
    kustomize_value = source.get("kustomize")
    directory_value = source.get("directory")
    return _ArgoSource(
        index=index,
        raw=source,
        repo_url=text(source.get("repoURL")),
        ref=text(source.get("targetRevision")) or "HEAD",
        path=path or ".",
        path_valid=path is not None,
        has_path=has_path,
        ref_name=text(source.get("ref")),
        chart=text(source.get("chart")),
        helm=as_mapping(helm_value),
        helm_present=helm_value is not None,
        kustomize=as_mapping(kustomize_value),
        kustomize_present=kustomize_value is not None,
        directory=as_mapping(directory_value),
    )


def _source_stub(index: int) -> _ArgoSource:
    return _parse_source(index, {})


def _owner_sources(sources: list[_ArgoSource]) -> list[_ArgoSource]:
    if len(sources) == 1:
        source = sources[0]
        if _is_ref_only(source):
            return []
        return [source]
    return [source for source in sources if not _is_ref_only(source)]


def _is_ref_only(source: _ArgoSource) -> bool:
    return (
        source.ref_name is not None
        and not source.has_path
        and source.chart is None
        and not source.helm_present
        and not source.kustomize_present
        and source.directory is None
    )


def _deployed_revisions(status: Mapping[str, Any], source_count: int) -> list[str | None]:
    sync = as_mapping(status.get("sync")) or {}
    operation_state = as_mapping(status.get("operationState")) or {}
    sync_result = as_mapping(operation_state.get("syncResult")) or {}
    revision_lists = (
        sync.get("revisions"),
        sync_result.get("revisions"),
    )
    for values in revision_lists:
        if isinstance(values, list) and len(values) == source_count:
            return [text(value) for value in values]

    if source_count == 1:
        revision = text(sync.get("revision")) or text(sync_result.get("revision"))
        return [revision]
    return [None] * source_count


def _application_workloads(
    status: Mapping[str, Any],
    metadata: Mapping[str, Any],
    destination_namespace: str | None,
    application_name: str,
    *,
    live_resources: Iterable[Mapping[str, Any]] = (),
) -> list[_ArgoWorkload]:
    raw_resources = status.get("resources")
    workloads: list[_ArgoWorkload] = []
    if isinstance(raw_resources, list):
        for raw_resource in raw_resources:
            resource = as_mapping(raw_resource)
            if resource is None:
                continue
            workload = _workload_from_resource(resource, destination_namespace)
            if workload is not None:
                workloads.append(workload)
        if raw_resources:
            return _unique_workloads(workloads)
    elif raw_resources is not None:
        return []

    live_fallbacks: list[_ArgoWorkload] = []
    for raw_resource in live_resources:
        resource = as_mapping(raw_resource)
        if resource is None:
            continue
        live_metadata = as_mapping(resource.get("metadata")) or {}
        tracking_id = _tracking_id(live_metadata)
        workload = (
            _workload_from_tracking_id(tracking_id, destination_namespace, application_name)
            if tracking_id is not None
            else None
        )
        if workload is not None:
            live_fallbacks.append(workload)
    if live_fallbacks:
        return _unique_workloads(live_fallbacks)

    tracking_id = _tracking_id(metadata)
    fallback = _workload_from_tracking_id(tracking_id, destination_namespace, application_name) if tracking_id else None
    if fallback is None:
        return []
    return [fallback]


def _workload_from_resource(
    resource: Mapping[str, Any],
    destination_namespace: str | None,
) -> _ArgoWorkload | None:
    kind = text(resource.get("kind"))
    name = text(resource.get("name"))
    if kind is None or kind not in WORKLOAD_KINDS or name is None:
        return None
    namespace = text(resource.get("namespace")) or destination_namespace
    if namespace is None:
        return None
    return _ArgoWorkload(
        kind=kind,
        namespace=namespace,
        name=name,
        group=text(resource.get("group")),
        version=text(resource.get("version")),
    )


def _unique_workloads(workloads: list[_ArgoWorkload]) -> list[_ArgoWorkload]:
    unique: dict[tuple[str | None, str | None, str, str, str], _ArgoWorkload] = {}
    for workload in workloads:
        key = (workload.group, workload.version, workload.kind, workload.namespace, workload.name)
        unique.setdefault(key, workload)
    return list(unique.values())


def _tracking_id(metadata: Mapping[str, Any]) -> str | None:
    for section in ("annotations", "labels"):
        values = as_mapping(metadata.get(section)) or {}
        tracking_id = text(values.get("argocd.argoproj.io/tracking-id"))
        if tracking_id is not None:
            return tracking_id
    return None


def _workload_from_tracking_id(
    tracking_id: str,
    destination_namespace: str | None,
    application_name: str,
) -> _ArgoWorkload | None:
    parts = tracking_id.rsplit(":", 2)
    if len(parts) != 3:
        return None
    application_prefix, group_kind, namespace_name = parts
    if application_prefix.split("/")[-1] != application_name:
        return None
    group, separator, kind = group_kind.partition("/")
    namespace, name_separator, name = namespace_name.partition("/")
    if not separator or not name_separator or not name or kind not in WORKLOAD_KINDS:
        return None
    if not namespace:
        namespace = destination_namespace or ""
    if not namespace:
        return None
    return _ArgoWorkload(
        kind=kind,
        namespace=namespace,
        name=name,
        group=group or None,
        version=None,
        tracking_id=tracking_id,
    )


def _candidate_for_workload(
    *,
    owner: _ArgoSource,
    sources: list[_ArgoSource],
    revisions: list[str | None],
    resource: _ArgoWorkload,
    cluster: str,
    provider_id: str,
    application_name: str,
    application_namespace: str,
    application_uid: str | None,
    application_generation: int | None,
    destination_namespace: str | None,
    helm_values_mapping: Mapping[str, Any] | None,
    helm_values_mapping_error: str | None,
    application_source: Mapping[str, Any] | None,
    kustomization_paths: set[str],
    application_source_type: str | None,
    forced_status: BindingStatus | None,
    forced_reason: str | None,
    controller_state: Mapping[str, Any],
    observed_at: datetime | None,
) -> WorkloadSource:
    renderer, renderer_config, renderer_error, inline_values = _renderer_configuration(
        owner=owner,
        sources=sources,
        revisions=revisions,
        application_name=application_name,
        destination_namespace=destination_namespace,
        helm_values_mapping=helm_values_mapping,
        helm_values_mapping_error=helm_values_mapping_error,
        kustomization_paths=kustomization_paths,
        application_source_type=application_source_type,
    )
    binding_source = _git_values_binding_source(owner, sources, renderer_config)
    source_revision = revisions[binding_source.index] if 0 <= binding_source.index < len(revisions) else None
    repo_url = binding_source.repo_url or "unresolved"
    provider = text(provider_id) or "unresolved"
    ref = binding_source.ref
    path = binding_source.path
    status = BindingStatus.RESOLVED
    reason: str | None = None
    resolved_revision = (
        immutable_revision(source_revision) if binding_source.chart is None or binding_source.has_path else None
    )

    if forced_status is not None:
        status = forced_status
        reason = forced_reason
    elif not owner.path_valid:
        status = BindingStatus.BLOCKED
        reason = "unsafe-source-path"
    elif owner.repo_url is None:
        status = BindingStatus.UNRESOLVED
        reason = "source-repository-unresolved"
    elif text(provider_id) is None:
        status = BindingStatus.UNRESOLVED
        reason = "provider-unresolved"
    elif resource.kind not in SUPPORTED_WORKLOAD_KINDS:
        status = BindingStatus.BLOCKED
        reason = "owner-resolution-required" if resource.kind in {"Pod", "ReplicaSet"} else "unsupported-workload-kind"
    elif renderer_error is not None:
        status = BindingStatus.BLOCKED
        reason = renderer_error
    elif inline_values:
        location, location_error = _application_source_location(application_source, provider)
        if location_error is not None:
            status = BindingStatus.BLOCKED
            reason = location_error
        elif location is None:
            status = BindingStatus.BLOCKED
            reason = "application-not-in-git"
        else:
            provider = location["provider_id"]
            repo_url = location["repo_url"]
            ref = location["ref"]
            path = location["path"]
            resolved_revision = immutable_revision(location.get("revision"))
            renderer_config["application_source"] = {
                "provider_id": provider,
                "repo_url": repo_url,
                "ref": ref,
                "path": path,
            }
            if "revision" in location:
                renderer_config["application_source"]["revision"] = location["revision"]
    elif renderer is RendererKind.HELM_VALUES and not renderer_config.get("values_files") and owner.chart is not None:
        status = BindingStatus.BLOCKED
        reason = "helm-values-not-resolvable"

    evidence: dict[str, Any] = {
        "application": {"name": application_name, "namespace": application_namespace},
        "resource": {
            "group": resource.group,
            "version": resource.version,
            "kind": resource.kind,
            "namespace": resource.namespace,
            "name": resource.name,
        },
        "source_index": binding_source.index,
        "source_ref": binding_source.ref_name,
        "chart_source_index": owner.index,
        "deployed_revisions": [
            {
                "repo_url": source.repo_url,
                "ref": source.ref,
                "revision": revisions[source.index] if 0 <= source.index < len(revisions) else None,
            }
            for source in sources
        ],
    }
    if resource.tracking_id is not None:
        evidence["tracking_id_fallback"] = True
        evidence["tracking_id"] = resource.tracking_id
    evidence.update({key: value for key, value in controller_state.items() if value is not None})
    return workload_source(
        cluster=cluster,
        workload_namespace=resource.namespace,
        workload_kind=resource.kind,
        workload_name=resource.name,
        provider_id=provider,
        repo_url=repo_url,
        ref=ref,
        revision=resolved_revision,
        path=path,
        controller_kind=ControllerKind.ARGOCD,
        controller_name=application_name,
        controller_namespace=application_namespace,
        controller_uid=application_uid,
        controller_generation=application_generation,
        source_revision=source_revision,
        renderer=renderer,
        renderer_config=renderer_config,
        status=status,
        evidence=evidence,
        reason=reason,
        observed_at=observed_at,
        identity_salt=f"source-index:{owner.index}",
    )


def _git_values_binding_source(
    owner: _ArgoSource,
    sources: list[_ArgoSource],
    renderer_config: Mapping[str, Any],
) -> _ArgoSource:
    """Use the unique Git-backed values source as the editable Helm binding."""
    if owner.chart is None:
        return owner

    source_indexes: set[int] = set()
    for entry in renderer_config.get("values_sources", []):
        if not isinstance(entry, Mapping) or entry.get("source_ref") is None:
            continue
        index = entry.get("source_index")
        if isinstance(index, int) and not isinstance(index, bool):
            source_indexes.add(index)
    if len(source_indexes) != 1:
        return owner

    source_index = next(iter(source_indexes))
    if source_index < 0 or source_index >= len(sources):
        return owner
    values_source = sources[source_index]
    if values_source.chart is not None or values_source.repo_url is None:
        return owner
    return values_source


def _renderer_configuration(
    *,
    owner: _ArgoSource,
    sources: list[_ArgoSource],
    revisions: list[str | None],
    application_name: str,
    destination_namespace: str | None,
    helm_values_mapping: Mapping[str, Any] | None,
    helm_values_mapping_error: str | None,
    kustomization_paths: set[str],
    application_source_type: str | None,
) -> tuple[RendererKind, dict[str, Any], str | None, bool]:
    if owner.raw.get("plugin") is not None:
        return RendererKind.RAW, {}, "unsupported-config-management-plugin", False
    if owner.directory is not None and owner.directory.get("jsonnet"):
        return RendererKind.RAW, {}, "unsupported-directory-renderer", False
    if owner.helm_present or owner.chart is not None:
        if owner.helm_present and owner.helm is None:
            return RendererKind.HELM_VALUES, {}, "unsupported-helm-config", False
        helm = owner.helm or {}
        if owner.chart is not None and owner.has_path:
            return RendererKind.HELM_VALUES, {}, "conflicting-chart-and-path", False
        if owner.kustomize_present:
            return RendererKind.HELM_VALUES, {}, "conflicting-renderer-config", False
        config: dict[str, Any] = {
            "chart": {
                "name": owner.chart,
                "version": owner.ref if owner.chart is not None else None,
                "repo": owner.repo_url,
            },
            "release_name": text(helm.get("releaseName")) or application_name,
            "release_namespace": text(helm.get("namespace")) or destination_namespace or "default",
        }
        value_files, value_sources, error = _resolve_value_files(owner, helm, sources, revisions)
        config["values_files"] = value_files
        config["values_sources"] = value_sources
        if helm_values_mapping_error is not None:
            return RendererKind.HELM_VALUES, config, helm_values_mapping_error, False
        if helm_values_mapping is not None:
            mapping_source_index = helm_values_mapping.get("source_index")
            mapping_file_path = helm_values_mapping["file_path"]
            value_file_sources = [
                value_source for value_source in value_sources if value_source.get("path") == mapping_file_path
            ]
            if mapping_source_index is None and len(value_file_sources) == 1:
                mapping_source_index = value_file_sources[0].get("source_index")
            if (
                mapping_source_index is None
                and len(sources) > 1
                and mapping_file_path != join_repo_path(owner.path, "values.yaml", allow_root=False)
            ):
                return RendererKind.HELM_VALUES, config, "helm-values-mapping-source-index-required", False
            if mapping_source_index is not None and mapping_source_index >= len(sources):
                return RendererKind.HELM_VALUES, config, "invalid-helm-values-mapping", False
            mapping_source_matches = any(
                value_source.get("source_index") == mapping_source_index
                and value_source.get("path") == mapping_file_path
                for value_source in value_file_sources
            )
            if mapping_source_index is None and len(sources) > 1:
                mapping_source_matches = (
                    mapping_file_path == join_repo_path(owner.path, "values.yaml", allow_root=False)
                    and mapping_source_index is None
                )
            if mapping_source_index == owner.index or mapping_source_matches:
                config_source_index = (
                    mapping_source_index if "source_index" in helm_values_mapping or len(sources) > 1 else None
                )
                config["values_mapping"] = {
                    **{key: value for key, value in helm_values_mapping.items() if key not in {"source_index"}},
                    **({"source_index": config_source_index} if config_source_index is not None else {}),
                }
            elif mapping_source_index is None and len(sources) == 1:
                config["values_mapping"] = {
                    key: value for key, value in helm_values_mapping.items() if key != "source_index"
                }
            else:
                return RendererKind.HELM_VALUES, config, "invalid-helm-values-mapping", False
        if "values" in helm:
            config["values"] = deepcopy(helm.get("values"))
        if "valuesObject" in helm:
            config["values_object"] = deepcopy(helm.get("valuesObject"))
        if "parameters" in helm:
            config["parameters"] = deepcopy(helm.get("parameters"))
        inline_values = _has_inline_values(helm)
        return RendererKind.HELM_VALUES, config, error, inline_values

    if (
        owner.kustomize_present
        or owner.path in kustomization_paths
        or application_source_type == RendererKind.KUSTOMIZE.value
    ):
        if owner.kustomize is None:
            if not owner.kustomize_present:
                kustomize: Mapping[str, Any] = {}
            else:
                return RendererKind.KUSTOMIZE, {}, "unsupported-kustomize-config", False
        else:
            kustomize = owner.kustomize
        config = {
            "overlay_path": owner.path,
            "kustomization_path": join_repo_path(owner.path, "kustomization.yaml"),
        }
        for key, config_key in (
            ("patches", "patches"),
            ("components", "components"),
            ("images", "images"),
            ("helmCharts", "helm_charts"),
        ):
            if key in kustomize:
                config[config_key] = deepcopy(kustomize[key])
        return RendererKind.KUSTOMIZE, config, None, False

    if owner.raw.get("directory") is not None and owner.directory is None:
        return RendererKind.RAW, {}, "unsupported-directory-config", False
    directory = owner.directory or {}
    return RendererKind.RAW, {"directory": deepcopy(dict(directory))}, None, False


def _has_inline_values(helm: Mapping[str, Any]) -> bool:
    values = text(helm.get("values"))
    values_object = helm.get("valuesObject")
    parameters = helm.get("parameters")
    return (
        bool(values)
        or (isinstance(values_object, Mapping) and bool(values_object))
        or (isinstance(parameters, list) and bool(parameters))
    )


def _resolve_value_files(
    owner: _ArgoSource,
    helm: Mapping[str, Any],
    sources: list[_ArgoSource],
    revisions: list[str | None],
) -> tuple[list[str], list[dict[str, Any]], str | None]:
    raw_values = helm.get("valueFiles", [])
    if raw_values is None:
        raw_values = []
    if not isinstance(raw_values, list):
        return [], [], "unsupported-value-files"

    by_ref: dict[str, list[_ArgoSource]] = defaultdict(list)
    for source in sources:
        if source.ref_name is not None:
            by_ref[source.ref_name].append(source)

    resolved_files: list[str] = []
    resolved_sources: list[dict[str, Any]] = []
    for order, raw_value in enumerate(raw_values):
        if isinstance(raw_value, str) and raw_value != raw_value.strip():
            return resolved_files, resolved_sources, "unsafe-values-file-path"
        value = text(raw_value)
        if value is None:
            return resolved_files, resolved_sources, "unsafe-values-file-path"
        source_ref: str | None = None
        if value.startswith("$"):
            ref_name, separator, referenced_path = value[1:].partition("/")
            if not separator or not ref_name or not referenced_path:
                return resolved_files, resolved_sources, "unresolved-values-reference"
            matches = by_ref.get(ref_name, [])
            if len(matches) != 1:
                return resolved_files, resolved_sources, "unresolved-values-reference"
            value_source = matches[0]
            if not value_source.path_valid or value_source.repo_url is None or value_source.chart is not None:
                return resolved_files, resolved_sources, "unresolved-values-reference"
            path = safe_repo_path(referenced_path, allow_root=False)
            source_ref = ref_name
        else:
            if not owner.has_path:
                return resolved_files, resolved_sources, "local-values-source-unresolved"
            value_source = owner
            path = join_repo_path(owner.path, value, allow_root=False)
        if path is None:
            return resolved_files, resolved_sources, "unsafe-values-file-path"
        resolved_files.append(path)
        resolved_sources.append(
            {
                "order": order,
                "source_ref": source_ref,
                "repo_url": value_source.repo_url,
                "ref": value_source.ref,
                "source_path": value_source.path,
                "path": path,
                "source_index": value_source.index,
                "source_revision": (
                    revisions[value_source.index] if 0 <= value_source.index < len(revisions) else None
                ),
            }
        )
    return resolved_files, resolved_sources, None


def _application_source_location(
    application_source: Mapping[str, Any] | None,
    default_provider_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    if application_source is None:
        return None, None
    repo_url = text(application_source.get("repo_url")) or text(application_source.get("repoURL"))
    provider_id = text(application_source.get("provider_id")) or default_provider_id
    ref = (
        text(application_source.get("ref"))
        or text(application_source.get("target_revision"))
        or text(application_source.get("targetRevision"))
        or "HEAD"
    )
    revision = text(application_source.get("revision"))
    raw_path = application_source.get("path")
    path = safe_repo_path(raw_path, allow_root=True)
    if path is None:
        return None, "unsafe-application-source-path"
    if repo_url is None or provider_id is None:
        return None, "application-not-in-git"
    location = {"provider_id": provider_id, "repo_url": repo_url, "ref": ref, "path": path}
    if revision is not None:
        location["revision"] = revision
    return location, None
