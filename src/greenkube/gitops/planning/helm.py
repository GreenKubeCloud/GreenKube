"""Render-validated planning for local Helm charts and Git-owned Flux releases."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Protocol

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString

from greenkube.gitops.contracts import (
    ActionPlanV3,
    AutomationErrorCode,
    BindingStatus,
    ContainerResources,
    ControllerKind,
    FileEdit,
    FilePlan,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
    canonical_repository_identity,
)
from greenkube.gitops.discovery.common import flux_release_name, text
from greenkube.gitops.planning.kustomize import (
    KustomizePlanError,
    _canonical_repo_path,
    _file_diff,
    _git_blob_sha,
    _read_snapshot_file,
    _snapshot_file_map,
    _snapshot_root,
    _stage_files,
    _validate_workspace_inventory,
)
from greenkube.gitops.planning.raw_manifest import (
    _POD_SPEC_PATHS,
    _WORKLOAD_API_VERSIONS,
    RawManifestPlanError,
    _apply_operations,
    _containers,
    _find_container,
    _order_operations_by_target_container,
    _plan_identifier,
    _pod_spec,
    _rendered_json,
    _resources_by_identity,
    _target_container_order,
    _target_identity,
    _unified_diff,
    _validate_operation_resources,
    _validate_rendered_change,
)
from greenkube.gitops.renderers.helm import HelmError, HelmRenderer
from greenkube.gitops.renderers.raw import RawRenderError, RawRenderResult, render_raw
from greenkube.gitops.snapshots.models import RepositorySnapshot as ArchiveSnapshot

DEFAULT_HELM_VERSION = "v3.22.1+gd6c1a0e"
_MAX_FLUX_VALUES_FILES = 32
_MAX_FLUX_VALUES_BYTES = 8_388_608
_FULL_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SNAPSHOT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HELMRELEASE_KIND = re.compile(r"(?m)^\s*kind\s*:\s*['\"]?HelmRelease(?:['\"])?\s*(?:#.*)?$")
_HELMRELEASE_JSON_KIND = re.compile(r'"kind"\s*:\s*"HelmRelease"')
_MANAGED_ANNOTATION = "greenkube.cloud/managed"
_ACTION_ID_ANNOTATION = "greenkube.cloud/action-id"


class HelmPlanError(ValueError):
    """A Helm source cannot be planned safely."""


class HelmValuesPlanError(HelmPlanError):
    """A local Helm values source cannot be edited or rendered safely."""


class HelmValuesNeedsMappingError(HelmValuesPlanError):
    """A chart mapping is required before a values-file plan can be created."""

    code = AutomationErrorCode.NEEDS_CHART_MAPPING
    reason_code = AutomationErrorCode.NEEDS_CHART_MAPPING.value


class HelmValuesUnsupportedError(HelmValuesPlanError):
    """The explicit values mapping is unsupported or fails render validation."""

    code = AutomationErrorCode.UNSUPPORTED_SOURCE


class FluxHelmReleasePlanError(HelmPlanError):
    """A Flux HelmRelease cannot be updated and render-validated safely."""


class FluxHelmReleaseUnsupportedError(FluxHelmReleasePlanError):
    """The Flux release source or post-renderer is outside the validated subset."""

    code = AutomationErrorCode.UNSUPPORTED_SOURCE


class _HelmRenderEngine(Protocol):
    def render(
        self,
        snapshot: ArchiveSnapshot,
        chart_path: str,
        *,
        release_name: str,
        release_namespace: str,
        values_files: Sequence[str] = (),
    ) -> RawRenderResult: ...


@dataclass(frozen=True, slots=True)
class _HelmContext:
    root: Path
    files: Mapping[str, Any]
    chart_path: str
    release_name: str
    release_namespace: str
    values_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HelmValuesMapping:
    """An explicit mapping from one Git-owned values file to one target container."""

    file_path: str
    container: str
    path: tuple[str, ...]
    source_index: int | None = None

    def __post_init__(self) -> None:
        try:
            normalized_path = tuple(self.path)
        except TypeError as exc:
            raise HelmValuesUnsupportedError("values mapping path must be a sequence of string keys") from exc
        object.__setattr__(self, "path", normalized_path)
        if not self.file_path or self.file_path != self.file_path.strip():
            raise HelmValuesUnsupportedError("values mapping file_path must be a non-blank canonical path")
        if not self.container or self.container != self.container.strip():
            raise HelmValuesUnsupportedError("values mapping container must be a non-blank name")
        if not self.path or any(not isinstance(item, str) or not item or item != item.strip() for item in self.path):
            raise HelmValuesUnsupportedError("values mapping path must contain non-blank string keys")
        if len(self.path) > 32:
            raise HelmValuesUnsupportedError("values mapping path is too deep")
        if self.path[-1] != "resources":
            raise HelmValuesUnsupportedError("values mapping path must identify a resources mapping")
        if self.source_index is not None and (
            isinstance(self.source_index, bool) or not isinstance(self.source_index, int) or self.source_index < 0
        ):
            raise HelmValuesUnsupportedError("values mapping source_index must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class _ManagedPostRendererPatch:
    target: ResourceTarget
    document: MutableMapping[str, Any]
    entry_index: int
    patch_index: int


@dataclass(slots=True)
class _ReleaseManifest:
    path: str
    content: str
    documents: list[Any]
    document_index: int
    yaml: YAML | None
    is_json: bool

    @property
    def document(self) -> MutableMapping[str, Any]:
        value = self.documents[self.document_index]
        if not isinstance(value, MutableMapping):
            raise FluxHelmReleasePlanError("the Git-owned HelmRelease document must be a mapping")
        return value


@dataclass(frozen=True, slots=True)
class _FluxHelmReleaseContext:
    operations: tuple[SetContainerResources, ...]
    root: Path
    files: Mapping[str, Any]
    release: _ReleaseManifest
    spec: Mapping[str, Any]
    helm: _HelmContext
    temporary_values: Mapping[str, str]


class HelmValuesPlanner:
    """Plan an explicit values-file mapping after render validation."""

    def __init__(
        self,
        *,
        renderer: _HelmRenderEngine | None = None,
        expected_version: str = DEFAULT_HELM_VERSION,
        binary: str = "helm",
    ) -> None:
        self.renderer = renderer or HelmRenderer(expected_version=expected_version, binary=binary)

    def render_source(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveSnapshot,
        base_revision: str,
        *,
        mapping: HelmValuesMapping | Mapping[str, Any] | None = None,
    ) -> RawRenderResult:
        """Render one validated local Argo CD chart snapshot without editing its files."""
        operations, context, _ = _prepare_helm_values_plan(
            action,
            source,
            snapshot,
            base_revision,
            mapping,
            renderer=self.renderer,
        )
        result = _render_chart(
            self.renderer,
            snapshot,
            context,
            source.renderer,
            HelmValuesUnsupportedError,
        )
        _require_exact_target(result, action.target, HelmValuesUnsupportedError)
        _require_target_containers(result, action.target, operations, HelmValuesUnsupportedError)
        return replace(result, renderer=RendererKind.HELM_VALUES)

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveSnapshot,
        base_revision: str,
        *,
        mapping: HelmValuesMapping | Mapping[str, Any] | None = None,
    ) -> FilePlan:
        if isinstance(base_revision, str):
            base_revision = base_revision.lower()
        operations, context, selected_mapping = _prepare_helm_values_plan(
            action,
            source,
            snapshot,
            base_revision,
            mapping,
            renderer=self.renderer,
        )
        before = _render_chart(
            self.renderer,
            snapshot,
            context,
            source.renderer,
            HelmValuesUnsupportedError,
        )
        _require_exact_target(before, action.target, HelmValuesUnsupportedError)
        _require_target_containers(before, action.target, operations, HelmValuesUnsupportedError)

        values_path = selected_mapping.file_path
        original_values = _read_source_file(context.root, context.files, values_path, HelmValuesUnsupportedError)
        updated_values = _edit_values_file(
            original_values,
            values_path,
            selected_mapping.path,
            operations,
        )
        if updated_values == original_values:
            raise HelmValuesPlanError("the requested container resources are already present in the values file")

        with _stage(context.root, context.files, {values_path: updated_values}, HelmValuesPlanError):
            after = _render_chart(
                self.renderer,
                snapshot,
                context,
                source.renderer,
                HelmValuesUnsupportedError,
            )
        before_resources = _resources_by_identity(before)
        after_resources = _resources_by_identity(after)
        target_identity = _target_identity(action.target)
        if before_resources[target_identity] == after_resources[target_identity]:
            raise HelmValuesUnsupportedError("explicit values mapping does not change the rendered target container")
        try:
            _validate_rendered_change(before, after, action.target, action.target.kind, operations)
        except RawManifestPlanError as exc:
            message = str(exc)
            if "exceeds its limit" in message or "invalid Kubernetes resource quantity" in message:
                raise HelmValuesPlanError(f"Helm values render validation failed: {exc}") from exc
            raise HelmValuesUnsupportedError(
                f"explicit values mapping changed unrelated resources or fields: {exc}"
            ) from exc

        file_edit = FileEdit(
            path=values_path,
            operation="update",
            original_sha=_git_blob_sha(original_values.encode("utf-8"), base_revision),
            content=updated_values,
        )
        return _file_plan(
            action=action,
            source=source,
            base_revision=base_revision,
            renderer=RendererKind.HELM_VALUES,
            before=before,
            after=after,
            edits=[file_edit],
            originals={values_path: original_values},
        )


class FluxHelmReleasePlanner:
    """Add one exact, managed post-renderer patch to a Git-owned Flux HelmRelease."""

    def __init__(
        self,
        *,
        renderer: _HelmRenderEngine | None = None,
        expected_version: str = DEFAULT_HELM_VERSION,
        binary: str = "helm",
    ) -> None:
        self.renderer = renderer or HelmRenderer(expected_version=expected_version, binary=binary)

    def render_source(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveSnapshot,
        base_revision: str,
        *,
        expected_manifest_path: str | None = None,
    ) -> RawRenderResult:
        """Render one exact Flux source using only the supported local HelmRelease subset."""
        revision = base_revision.lower() if isinstance(base_revision, str) else base_revision
        prepared = _prepare_flux_helm_release_context(
            action,
            source,
            snapshot,
            revision,
            expected_manifest_path=expected_manifest_path,
        )
        with _stage(
            prepared.root,
            prepared.files,
            prepared.temporary_values,
            FluxHelmReleaseUnsupportedError,
        ):
            chart = _render_chart(
                self.renderer,
                snapshot,
                prepared.helm,
                RendererKind.HELM_RELEASE,
                FluxHelmReleaseUnsupportedError,
            )
            _require_exact_target(chart, action.target, FluxHelmReleaseUnsupportedError)
            patches = _managed_postrenderer_patches(prepared.spec, FluxHelmReleaseUnsupportedError)
            result = _apply_managed_postrenderer_patches(chart, patches, FluxHelmReleaseUnsupportedError)
            _require_target_containers(
                result,
                action.target,
                prepared.operations,
                FluxHelmReleaseUnsupportedError,
            )
        return result

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: ArchiveSnapshot,
        base_revision: str,
    ) -> FilePlan:
        if isinstance(base_revision, str):
            base_revision = base_revision.lower()
        prepared = _prepare_flux_helm_release_context(
            action,
            source,
            snapshot,
            base_revision,
            expected_manifest_path=None,
        )
        operations = prepared.operations
        root = prepared.root
        snapshot_files = prepared.files
        parsed_release = prepared.release
        spec = prepared.spec
        context = prepared.helm

        with _stage(root, snapshot_files, prepared.temporary_values, FluxHelmReleaseUnsupportedError):
            before_chart = _render_chart(
                self.renderer,
                snapshot,
                context,
                RendererKind.HELM_RELEASE,
                FluxHelmReleaseUnsupportedError,
            )
            _require_exact_target(before_chart, action.target, FluxHelmReleaseUnsupportedError)
            existing_patches = _managed_postrenderer_patches(spec, FluxHelmReleaseUnsupportedError)
            before = _apply_managed_postrenderer_patches(
                before_chart,
                existing_patches,
                FluxHelmReleaseUnsupportedError,
            )
            _require_target_containers(before, action.target, operations, FluxHelmReleaseUnsupportedError)
            container_order = _target_container_order(before_chart, action.target) or ()
            operations = _order_operations_by_target_container(before_chart, action.target, operations)

            updated_release_content = _update_release_postrenderer(
                parsed_release,
                action,
                operations,
                existing_patches,
                container_order=container_order,
            )
            if updated_release_content == parsed_release.content:
                raise FluxHelmReleasePlanError("the requested Flux post-renderer patch is already present")

            with _stage(
                root,
                snapshot_files,
                {parsed_release.path: updated_release_content},
                FluxHelmReleaseUnsupportedError,
            ):
                after_chart = _render_chart(
                    self.renderer,
                    snapshot,
                    context,
                    RendererKind.HELM_RELEASE,
                    FluxHelmReleaseUnsupportedError,
                )
                _require_exact_target(after_chart, action.target, FluxHelmReleaseUnsupportedError)
                updated_documents = _parse_release_content(
                    parsed_release.path,
                    updated_release_content,
                    FluxHelmReleasePlanError,
                )
                updated_manifest = _manifest_from_documents(
                    parsed_release.path,
                    updated_documents,
                    parsed_release.document_index,
                )
                updated_spec = _mapping(updated_manifest.get("spec"))
                if updated_spec is None:
                    raise FluxHelmReleasePlanError("updated HelmRelease spec must be a mapping")
                updated_patches = _managed_postrenderer_patches(updated_spec, FluxHelmReleaseUnsupportedError)
                after = _apply_managed_postrenderer_patches(
                    after_chart,
                    updated_patches,
                    FluxHelmReleaseUnsupportedError,
                )

        try:
            _validate_flux_rendered_change(
                before,
                after,
                action.target,
                action.target.kind,
                operations,
                action.action_id,
            )
        except FluxHelmReleasePlanError as exc:
            raise FluxHelmReleaseUnsupportedError(f"Flux post-renderer mapping is unsupported: {exc}") from exc
        before_resources = _resources_by_identity(before)
        after_resources = _resources_by_identity(after)
        target_identity = _target_identity(action.target)
        if _operation_resources(
            before_resources[target_identity], action.target.kind, operations
        ) == _operation_resources(
            after_resources[target_identity],
            action.target.kind,
            operations,
        ):
            raise FluxHelmReleasePlanError("the requested container resources are already present after post-rendering")

        file_edit = FileEdit(
            path=parsed_release.path,
            operation="update",
            original_sha=_git_blob_sha(parsed_release.content.encode("utf-8"), base_revision),
            content=updated_release_content,
        )
        return _file_plan(
            action=action,
            source=source,
            base_revision=base_revision,
            renderer=RendererKind.HELM_RELEASE,
            before=before,
            after=after,
            edits=[file_edit],
            originals={parsed_release.path: parsed_release.content},
        )


def _prepare_flux_helm_release_context(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    base_revision: str,
    *,
    expected_manifest_path: str | None,
) -> _FluxHelmReleaseContext:
    operations, root, snapshot_files = _validate_common_request(
        action,
        source,
        snapshot,
        base_revision,
        RendererKind.HELM_RELEASE,
        FluxHelmReleasePlanError,
    )
    if source.controller_kind is not ControllerKind.FLUX_HELM_RELEASE:
        raise FluxHelmReleasePlanError("source must be a Flux HelmRelease binding")
    release_source = _validate_flux_manifest_owner(source, base_revision)
    parsed_release = _find_release_manifest(root, snapshot_files, source, release_source)
    if expected_manifest_path is not None:
        expected_path = _canonical_path(
            expected_manifest_path,
            allow_root=False,
            error=FluxHelmReleaseUnsupportedError,
        )
        if parsed_release.path != expected_path:
            raise FluxHelmReleaseUnsupportedError(
                "the Git-owned HelmRelease manifest path changed after the forward plan"
            )
    spec = _mapping(parsed_release.document.get("spec"))
    if spec is None:
        raise FluxHelmReleasePlanError("Git-owned HelmRelease spec must be a mapping")
    (
        chart_path,
        release_name,
        release_namespace,
        chart_values_files,
        ignore_missing_values_files,
    ) = _flux_chart_context(
        root,
        snapshot_files,
        source,
        spec,
        base_revision,
    )
    values_from = _load_flux_values_from(root, snapshot_files, source, spec, base_revision)
    inline_values = spec.get("values")
    if inline_values is None:
        inline_values = {}
    if not isinstance(inline_values, Mapping):
        raise FluxHelmReleaseUnsupportedError("HelmRelease spec.values must be a mapping")
    inline_values = _plain_flux_values(inline_values)
    merged_values = _merge_flux_value_maps(values_from, inline_values)
    temporary_values: dict[str, str] = {}
    if chart_values_files:
        default_values_path = _join_path(chart_path, "values.yaml")
        if default_values_path not in snapshot_files:
            raise FluxHelmReleaseUnsupportedError(
                "Flux chart.spec.valuesFiles cannot be applied because the local chart has no values.yaml"
            )
        chart_defaults = _load_flux_chart_values_files(
            root,
            snapshot_files,
            chart_values_files,
            ignore_missing=ignore_missing_values_files,
        )
        temporary_values[default_values_path] = _safe_dump_values(chart_defaults)

    overlay_path = None
    if merged_values:
        overlay_path = _temporary_values_path(snapshot_files, action, source, base_revision)
        temporary_values[overlay_path] = _safe_dump_values(merged_values)
    helm = _HelmContext(
        root=root,
        files=snapshot_files,
        chart_path=chart_path,
        release_name=release_name,
        release_namespace=release_namespace,
        values_files=(overlay_path,) if overlay_path is not None else (),
    )
    return _FluxHelmReleaseContext(
        operations=tuple(operations),
        root=root,
        files=snapshot_files,
        release=parsed_release,
        spec=spec,
        helm=helm,
        temporary_values=temporary_values,
    )


def plan_helm_values(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    base_revision: str,
    *,
    renderer: _HelmRenderEngine | None = None,
    expected_version: str = DEFAULT_HELM_VERSION,
    mapping: HelmValuesMapping | Mapping[str, Any] | None = None,
) -> FilePlan:
    """Create a render-validated Helm values FilePlan."""

    return HelmValuesPlanner(renderer=renderer, expected_version=expected_version).plan(
        action,
        source,
        snapshot,
        base_revision,
        mapping=mapping,
    )


def plan_flux_helm_release(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    base_revision: str,
    *,
    renderer: _HelmRenderEngine | None = None,
    expected_version: str = DEFAULT_HELM_VERSION,
) -> FilePlan:
    """Create a render-validated Flux HelmRelease post-renderer FilePlan."""

    return FluxHelmReleasePlanner(renderer=renderer, expected_version=expected_version).plan(
        action,
        source,
        snapshot,
        base_revision,
    )


def _prepare_helm_values_plan(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    base_revision: str,
    mapping: HelmValuesMapping | Mapping[str, Any] | None,
    *,
    renderer: _HelmRenderEngine,
) -> tuple[list[SetContainerResources], _HelmContext, HelmValuesMapping]:
    operations, root, snapshot_files = _validate_common_request(
        action,
        source,
        snapshot,
        base_revision,
        RendererKind.HELM_VALUES,
        HelmValuesPlanError,
    )
    if len(operations) != 1:
        raise HelmValuesUnsupportedError("values planning supports exactly one explicitly mapped container")
    if source.controller_kind is not ControllerKind.ARGOCD:
        raise HelmValuesUnsupportedError("Helm values planning is supported only for Argo CD sources")
    config = source.renderer_config
    chart_config = _mapping(config.get("chart"))
    if chart_config is None:
        raise HelmValuesUnsupportedError("Helm renderer configuration must include chart metadata")
    if chart_config.get("name") not in (None, ""):
        raise HelmValuesUnsupportedError("remote Helm charts cannot be rendered from this repository snapshot")
    chart_repository = chart_config.get("repo")
    if chart_repository not in (None, "", source.repo_url):
        raise HelmValuesUnsupportedError("remote or cross-repository Helm charts cannot be planned")
    _reject_inline_helm_values(config)

    chart_path = _canonical_path(source.path, allow_root=True, error=HelmValuesUnsupportedError)
    if not _chart_exists(snapshot_files, chart_path):
        raise HelmValuesUnsupportedError("local Helm chart is not available in the repository snapshot")
    chart_metadata_path = _join_path(chart_path, "Chart.yaml")
    chart_metadata_content = _read_source_file(
        root,
        snapshot_files,
        chart_metadata_path,
        HelmValuesUnsupportedError,
    )
    chart_metadata, _, _ = _load_values(
        chart_metadata_content,
        chart_metadata_path,
        HelmValuesUnsupportedError,
        round_trip=False,
    )
    if not isinstance(chart_metadata, Mapping):
        raise HelmValuesUnsupportedError("local chart metadata must be a mapping")
    _reject_chart_dependencies(chart_metadata, HelmValuesUnsupportedError)

    raw_values_files = config.get("values_files", [])
    if raw_values_files is None:
        raw_values_files = []
    if not isinstance(raw_values_files, list):
        raise HelmValuesUnsupportedError("Argo values_files must be a list")
    values_files = tuple(
        _canonical_path(path, allow_root=False, error=HelmValuesUnsupportedError) for path in raw_values_files
    )
    if len(set(values_files)) != len(values_files):
        raise HelmValuesUnsupportedError("Helm values file order contains duplicate paths")
    _validate_argo_values_sources(source, base_revision)
    for values_path in values_files:
        if values_path not in snapshot_files:
            raise HelmValuesUnsupportedError(
                f"{values_path}: Helm values file is unavailable in the repository snapshot"
            )
        if PurePosixPath(values_path).suffix.lower() not in {".yaml", ".yml", ".json"}:
            raise HelmValuesUnsupportedError(f"{values_path}: only YAML and JSON Helm values files are supported")

    release_name = config.get("release_name")
    release_namespace = config.get("release_namespace")
    if not isinstance(release_name, str) or not release_name.strip():
        raise HelmValuesUnsupportedError("Helm release_name must be explicit")
    if not isinstance(release_namespace, str) or not release_namespace.strip():
        raise HelmValuesUnsupportedError("Helm release_namespace must be explicit")
    context = _HelmContext(
        root=root,
        files=snapshot_files,
        chart_path=chart_path,
        release_name=release_name,
        release_namespace=release_namespace,
        values_files=values_files,
    )
    mapping_files = values_files
    if not mapping_files:
        default_values_path = _join_path(chart_path, "values.yaml")
        if default_values_path in snapshot_files:
            mapping_files = (default_values_path,)

    configured_mapping = config.get("values_mapping")
    if mapping is not None or configured_mapping is not None:
        selected_mapping = _select_values_mapping(
            mapping,
            configured_mapping,
            mapping_files,
            snapshot_files,
            operations[0].container,
        )
    else:
        selected_mapping = _infer_values_mapping(
            action,
            operations,
            source,
            snapshot,
            context,
            mapping_files,
            snapshot_files,
            renderer,
        )
    _validate_values_mapping_source(source, selected_mapping, mapping_files)
    return operations, context, selected_mapping


def _validate_common_request(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    base_revision: str,
    expected_renderer: RendererKind,
    error_type: type[HelmPlanError],
) -> tuple[list[SetContainerResources], Path, Mapping[str, Any]]:
    if not isinstance(action, ActionPlanV3):
        raise error_type("action must be an ActionPlanV3")
    if not isinstance(source, WorkloadSource):
        raise error_type("source must be a resolved WorkloadSource")
    if not isinstance(snapshot, ArchiveSnapshot):
        raise error_type("snapshot must be an archive RepositorySnapshot")
    if not isinstance(base_revision, str) or _FULL_SHA.fullmatch(base_revision) is None:
        raise error_type("resolved base revision must be a full 40- or 64-character commit SHA")
    if base_revision.lower() != snapshot.metadata.commit_sha.lower():
        raise error_type("base revision does not match the archive snapshot commit")
    if source.provider_id != snapshot.metadata.provider_id or source.repo_url != snapshot.metadata.repo_url:
        raise error_type("workload source repository does not match the archive snapshot identity")
    if source.renderer is not expected_renderer:
        raise error_type(f"workload source must use the {expected_renderer.value} renderer")
    if source.status is not BindingStatus.RESOLVED:
        raise error_type("workload source binding must be resolved")
    if (
        action.target.kind != source.workload_kind
        or action.target.namespace != source.namespace
        or action.target.name != source.workload_name
    ):
        raise error_type("action target must match the resolved workload binding")
    supported_versions = _WORKLOAD_API_VERSIONS.get(action.target.kind)
    if supported_versions is None or action.target.api_version not in supported_versions:
        raise error_type(f"unsupported workload kind or API version: {action.target.api_version}/{action.target.kind}")
    if not action.operations or any(not isinstance(item, SetContainerResources) for item in action.operations):
        raise error_type("only explicit per-container rightsizing operations are supported")
    operations = [item for item in action.operations if isinstance(item, SetContainerResources)]
    if len({item.container for item in operations}) != len(operations):
        raise error_type("each container may have only one per-container rightsizing operation")
    if any(not item.resources.requests and not item.resources.limits for item in operations):
        raise error_type("each rightsizing operation must set at least one resource quantity")
    try:
        root = _snapshot_root(snapshot)
        snapshot_files = _snapshot_file_map(snapshot)
        _validate_workspace_inventory(root, snapshot_files)
    except KustomizePlanError as exc:
        raise error_type(f"repository snapshot is invalid: {exc}") from exc
    return operations, root, snapshot_files


def _validate_argo_values_sources(source: WorkloadSource, base_revision: str) -> None:
    raw_sources = source.renderer_config.get("values_sources", [])
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise HelmValuesUnsupportedError("Argo values_sources must be a list")
    for item in raw_sources:
        if not isinstance(item, Mapping):
            raise HelmValuesUnsupportedError("Argo values source metadata must be a mapping")
        if item.get("repo_url") != source.repo_url or item.get("ref") != source.ref:
            raise HelmValuesUnsupportedError("cross-repository or remote Helm values sources are unsupported")
        revision = item.get("source_revision")
        if revision is not None and not _revision_matches_snapshot(revision, base_revision):
            raise HelmValuesUnsupportedError("Helm values source revision does not match the repository snapshot")


def _reject_inline_helm_values(config: Mapping[str, Any]) -> None:
    for key in ("values", "values_object", "parameters"):
        value = config.get(key)
        if value not in (None, "", [], {}):
            raise HelmValuesUnsupportedError(
                "inline Helm values and parameters are unsupported until their Git-owned manifest path is resolved"
            )


def _reject_chart_dependencies(
    chart_metadata: Mapping[str, Any],
    error_type: type[HelmPlanError],
) -> None:
    dependencies = chart_metadata.get("dependencies", [])
    if dependencies not in (None, []):
        raise error_type("charts with dependencies are outside the safely rendered planning subset")


def _infer_values_mapping(
    action: ActionPlanV3,
    operations: Sequence[SetContainerResources],
    source: WorkloadSource,
    snapshot: ArchiveSnapshot,
    context: _HelmContext,
    mapping_files: Sequence[str],
    snapshot_files: Mapping[str, Any],
    renderer: _HelmRenderEngine,
) -> HelmValuesMapping:
    if not mapping_files:
        raise HelmValuesNeedsMappingError("needs_chart_mapping")

    file_path = mapping_files[-1]
    before = _render_chart(renderer, snapshot, context, source.renderer, HelmValuesUnsupportedError)
    _require_exact_target(before, action.target, HelmValuesUnsupportedError)
    _require_target_containers(before, action.target, operations, HelmValuesUnsupportedError)
    try:
        _validate_rendered_change(before, before, action.target, action.target.kind, list(operations))
    except RawManifestPlanError as exc:
        _raise_resource_validation_error(exc, skip_unsupported=True)
    else:
        raise HelmValuesPlanError("the requested container resources are already present in the rendered chart")

    original_values = _read_source_file(context.root, snapshot_files, file_path, HelmValuesUnsupportedError)
    document, _, _ = _load_values(original_values, file_path, HelmValuesUnsupportedError, round_trip=False)
    if not isinstance(document, Mapping):
        raise HelmValuesUnsupportedError(f"{file_path}: Helm values root must be a mapping")

    candidate_paths = _resource_values_paths(document)
    if not candidate_paths or len(candidate_paths) > 64:
        raise HelmValuesNeedsMappingError("needs_chart_mapping")

    matches: list[HelmValuesMapping] = []
    for path in candidate_paths:
        try:
            updated_values = _edit_values_file(original_values, file_path, path, operations)
        except HelmValuesUnsupportedError:
            continue
        if updated_values == original_values:
            continue

        try:
            with _stage(context.root, snapshot_files, {file_path: updated_values}, HelmValuesPlanError):
                after = _render_chart(renderer, snapshot, context, source.renderer, HelmValuesUnsupportedError)
            _require_exact_target(after, action.target, HelmValuesUnsupportedError)
            _require_target_containers(after, action.target, operations, HelmValuesUnsupportedError)
            _validate_rendered_change(before, after, action.target, action.target.kind, list(operations))
        except HelmValuesUnsupportedError:
            continue
        except RawManifestPlanError as exc:
            _raise_resource_validation_error(exc, skip_unsupported=True)
            continue

        source_index, source_index_ambiguous = _inferred_values_source_index(source, file_path)
        if source_index_ambiguous:
            raise HelmValuesNeedsMappingError("needs_chart_mapping")
        matches.append(
            HelmValuesMapping(
                file_path=file_path,
                container=operations[0].container,
                path=path,
                source_index=source_index,
            )
        )

    if len(matches) != 1:
        raise HelmValuesNeedsMappingError("needs_chart_mapping")
    return matches[0]


def _resource_values_paths(document: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    paths: set[tuple[str, ...]] = set()
    stack: list[tuple[Mapping[str, Any], tuple[str, ...], frozenset[int]]] = [(document, (), frozenset())]
    visited_nodes = 0
    visited_entries = 0
    while stack:
        mapping, parent_path, ancestors = stack.pop()
        identity = id(mapping)
        if identity in ancestors:
            continue
        visited_nodes += 1
        if visited_nodes > 10_000:
            return ()
        next_ancestors = ancestors | {identity}
        for key, value in mapping.items():
            visited_entries += 1
            if visited_entries > 50_000:
                return ()
            if not isinstance(key, str) or not key or len(parent_path) >= 32:
                continue
            path = (*parent_path, key)
            if key == "resources" and isinstance(value, Mapping):
                paths.add(path)
            if isinstance(value, Mapping):
                stack.append((value, path, next_ancestors))
    return tuple(sorted(paths))


def _inferred_values_source_index(
    source: WorkloadSource,
    file_path: str,
) -> tuple[int | None, bool]:
    raw_sources = source.renderer_config.get("values_sources", [])
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise HelmValuesUnsupportedError("Argo values_sources must be a list")
    source_indexes: set[int] = set()
    for item in raw_sources:
        if not isinstance(item, Mapping) or item.get("path") != file_path:
            continue
        source_index = item.get("source_index")
        if isinstance(source_index, int) and not isinstance(source_index, bool) and source_index >= 0:
            source_indexes.add(source_index)
    if len(source_indexes) > 1:
        return None, True
    return (next(iter(source_indexes)) if source_indexes else None), False


def _raise_resource_validation_error(exc: RawManifestPlanError, *, skip_unsupported: bool = False) -> None:
    message = str(exc)
    if "exceeds its limit" in message or "invalid Kubernetes resource quantity" in message:
        raise HelmValuesPlanError(f"Helm values render validation failed: {exc}") from exc
    if not skip_unsupported:
        raise HelmValuesUnsupportedError(f"Helm values render validation failed: {exc}") from exc


def _validate_values_mapping_source(
    source: WorkloadSource,
    mapping: HelmValuesMapping,
    mapping_files: Sequence[str],
) -> None:
    if mapping.source_index is None:
        return
    raw_sources = source.renderer_config.get("values_sources", [])
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise HelmValuesUnsupportedError("Argo values_sources must be a list")
    if any(
        isinstance(item, Mapping)
        and isinstance(item.get("source_index"), int)
        and not isinstance(item.get("source_index"), bool)
        and item.get("source_index") == mapping.source_index
        and item.get("path") == mapping.file_path
        for item in raw_sources
    ):
        return
    owner_index = source.evidence.get("source_index")
    default_values_path = _join_path(source.path, "values.yaml")
    if (
        mapping.file_path in mapping_files
        and mapping.file_path == default_values_path
        and mapping.source_index == owner_index
        and not raw_sources
    ):
        return
    raise HelmValuesUnsupportedError("values mapping source_index does not identify its selected values file")


def _select_values_mapping(
    requested: HelmValuesMapping | Mapping[str, Any] | None,
    configured: object,
    values_files: Sequence[str],
    snapshot_files: Mapping[str, Any],
    target_container: str,
) -> HelmValuesMapping:
    requested_mapping = _coerce_values_mapping(requested)
    configured_mapping = _coerce_values_mapping(configured)
    if requested_mapping is not None and configured_mapping is not None:
        if (
            requested_mapping.file_path != configured_mapping.file_path
            or requested_mapping.container != configured_mapping.container
            or requested_mapping.path != configured_mapping.path
            or (
                requested_mapping.source_index is not None
                and configured_mapping.source_index is not None
                and requested_mapping.source_index != configured_mapping.source_index
            )
        ):
            raise HelmValuesUnsupportedError("conflicting explicit Helm values mappings were supplied")
        mapping = requested_mapping
        if mapping.source_index is None and configured_mapping.source_index is not None:
            mapping = configured_mapping
    else:
        mapping = requested_mapping or configured_mapping
    if mapping is None:
        raise HelmValuesNeedsMappingError("needs_chart_mapping")
    file_path = _canonical_path(
        mapping.file_path,
        allow_root=False,
        error=HelmValuesUnsupportedError,
    )
    if file_path not in values_files or file_path != values_files[-1]:
        raise HelmValuesUnsupportedError(
            "the explicit values mapping must target the final Git-managed values file in render order"
        )
    if file_path not in snapshot_files:
        raise HelmValuesUnsupportedError("the explicit values mapping file is unavailable in the snapshot")
    if mapping.container != target_container:
        raise HelmValuesUnsupportedError("the explicit values mapping does not name the exact action container")
    return HelmValuesMapping(
        file_path=file_path,
        container=mapping.container,
        path=mapping.path,
        source_index=mapping.source_index,
    )


def _coerce_values_mapping(value: object) -> HelmValuesMapping | None:
    if value is None:
        return None
    if isinstance(value, HelmValuesMapping):
        return value
    if not isinstance(value, Mapping):
        raise HelmValuesUnsupportedError("values_mapping must be an explicit mapping object")
    if set(value) - {"file_path", "container", "path", "source_index"}:
        raise HelmValuesUnsupportedError("values_mapping contains unsupported fields")
    file_path = value.get("file_path")
    container = value.get("container")
    path = value.get("path")
    source_index = value.get("source_index")
    if not isinstance(file_path, str) or not isinstance(container, str) or not isinstance(path, (list, tuple)):
        raise HelmValuesUnsupportedError("values_mapping requires file_path, container, and a string-key path")
    try:
        return HelmValuesMapping(
            file_path=file_path,
            container=container,
            path=tuple(path),
            source_index=source_index,
        )
    except (TypeError, ValueError) as exc:
        raise HelmValuesUnsupportedError(f"values_mapping is unsupported: {exc}") from exc


def _revision_matches_snapshot(value: object, revision: str) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower()
    return (
        normalized == revision.lower()
        or normalized.endswith(f"sha1:{revision.lower()}")
        or normalized.endswith(f"sha256:{revision.lower()}")
    )


def _edit_values_file(
    content: str,
    path: str,
    values_path: tuple[str, ...],
    operations: Sequence[SetContainerResources],
) -> str:
    document, yaml, is_json = _load_values(content, path, HelmValuesUnsupportedError, round_trip=True)
    if not isinstance(document, MutableMapping):
        raise HelmValuesUnsupportedError(f"{path}: Helm values root must be a mapping")
    resources = _mapping_at_path(document, values_path)
    if resources is None:
        raise HelmValuesUnsupportedError(f"{path}: explicit values path is not a mapping in the Git-owned file")
    for operation in operations:
        for field_name, quantities in (
            ("requests", operation.resources.requests),
            ("limits", operation.resources.limits),
        ):
            if quantities:
                _merge_resource_section(resources, field_name, quantities, path)
    return _dump_values(document, content, yaml, path, is_json)


def _merge_resource_section(
    resources: MutableMapping[str, Any],
    field_name: str,
    quantities: Mapping[str, str],
    path: str,
) -> None:
    section = resources.get(field_name)
    if section is None:
        section = CommentedMap()
        resources[field_name] = section
    elif not isinstance(section, MutableMapping):
        raise HelmValuesUnsupportedError(f"{path}: explicit resource values {field_name} must be a mapping")
    for resource_name in sorted(quantities):
        section[resource_name] = quantities[resource_name]


def _mapping_at_path(
    document: MutableMapping[str, Any],
    path: tuple[str, ...],
) -> MutableMapping[str, Any] | None:
    current: Any = document
    for token in path:
        if not isinstance(current, Mapping) or token not in current:
            return None
        current = current[token]
    return current if isinstance(current, MutableMapping) else None


def _load_values(
    content: str,
    path: str,
    error_type: type[HelmPlanError],
    *,
    round_trip: bool,
) -> tuple[Any, YAML | None, bool]:
    is_json = PurePosixPath(path).suffix.lower() == ".json"
    if is_json:
        try:
            value = json.loads(content, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, _DuplicateJSONKey, TypeError, ValueError) as exc:
            raise error_type(f"{path}: Helm JSON values could not be parsed") from exc
        return value, None, True
    yaml = YAML(typ="rt" if round_trip else "safe")
    yaml.allow_duplicate_keys = False
    if round_trip:
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        if "\r\n" in content and "\n" not in content.replace("\r\n", ""):
            yaml.line_break = "\r\n"
    try:
        value = yaml.load(content)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise error_type(f"{path}: Helm YAML values could not be parsed") from exc
    return value, yaml if round_trip else None, False


def _dump_values(document: Any, original: str, yaml: YAML | None, path: str, is_json: bool) -> str:
    if is_json:
        try:
            updated = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HelmValuesUnsupportedError(f"{path}: Helm JSON values could not be serialized") from exc
        if original.endswith(("\n", "\r")):
            updated += "\n"
        return updated
    if yaml is None:
        raise HelmValuesUnsupportedError(f"{path}: YAML serializer is unavailable")
    stream = io.StringIO()
    try:
        yaml.dump(document, stream)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise HelmValuesUnsupportedError(f"{path}: Helm YAML values could not be serialized") from exc
    updated = stream.getvalue()
    if original.endswith(("\n", "\r")):
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = updated.rstrip("\r\n")
    return updated


def _safe_dump_values(values: Mapping[str, Any]) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False
    yaml.allow_unicode = True
    yaml.width = 4096
    stream = io.StringIO()
    try:
        yaml.dump(_plain(dict(values)), stream)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise FluxHelmReleasePlanError(
            "HelmRelease inline values could not be serialized for render validation"
        ) from exc
    return stream.getvalue()


class _DuplicateJSONKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _render_chart(
    renderer: _HelmRenderEngine,
    snapshot: ArchiveSnapshot,
    context: _HelmContext,
    planner_renderer: RendererKind,
    error_type: type[HelmPlanError],
) -> RawRenderResult:
    try:
        result = renderer.render(
            snapshot,
            context.chart_path,
            release_name=context.release_name,
            release_namespace=context.release_namespace,
            values_files=context.values_files,
        )
    except HelmError as exc:
        raise error_type(f"bounded Helm render failed closed: {exc}") from exc
    if (
        not isinstance(result, RawRenderResult)
        or result.renderer is not RendererKind.HELM_RELEASE
        or not isinstance(result.output_digest, str)
        or _SNAPSHOT_DIGEST.fullmatch(result.output_digest) is None
    ):
        raise error_type("Helm renderer did not return a validated render result")
    if planner_renderer not in {RendererKind.HELM_VALUES, RendererKind.HELM_RELEASE}:
        raise error_type("Helm planner renderer kind is invalid")
    return result


def _require_exact_target(
    result: RawRenderResult,
    target: ResourceTarget,
    error_type: type[HelmPlanError],
) -> None:
    identities = [_target_identity(resource.target) for resource in result.resources]
    if len(identities) != len(set(identities)):
        raise error_type("Helm render contains ambiguous resource identities")
    matches = [resource for resource in result.resources if resource.target == target]
    if not matches:
        raise error_type("the exact workload target was not produced by the local Helm chart")
    if len(matches) != 1:
        raise error_type("the local Helm chart produced an ambiguous exact workload target")


def _require_target_containers(
    result: RawRenderResult,
    target: ResourceTarget,
    operations: Sequence[SetContainerResources],
    error_type: type[HelmPlanError],
) -> None:
    try:
        manifest = _resources_by_identity(result)[_target_identity(target)]
        containers = _containers(_pod_spec(manifest, target.kind))
        for operation in operations:
            _find_container(containers, operation.container)
    except (KeyError, RawManifestPlanError) as exc:
        raise error_type(f"the exact target container could not be resolved in the Helm render: {exc}") from exc


def _file_plan(
    *,
    action: ActionPlanV3,
    source: WorkloadSource,
    base_revision: str,
    renderer: RendererKind,
    before: RawRenderResult,
    after: RawRenderResult,
    edits: list[FileEdit],
    originals: Mapping[str, str],
) -> FilePlan:
    edits = sorted(edits, key=lambda item: item.path)
    diff = _file_diff(edits, originals)
    rendered_diff = _unified_diff(
        _rendered_json(before),
        _rendered_json(after),
        "rendered-before",
        "rendered-after",
    )
    warnings = sorted(set(before.warnings + after.warnings))
    source_revision = source.source_revision or source.revision
    validation = ValidationResult(
        target_changed=True,
        unrelated_changes=False,
        requests_within_limits=True,
        renderer=renderer,
        output_digest=after.output_digest,
        warnings=warnings,
    )
    plan_id = _plan_identifier(action, source, base_revision)
    digest = _plan_digest(
        plan_id=plan_id,
        action=action,
        source=source,
        edits=edits,
        diff=diff,
        rendered_diff=rendered_diff,
        base_revision=base_revision,
        source_revision=source_revision,
        validation=validation,
        warnings=warnings,
        renderer=renderer,
    )
    return FilePlan(
        plan_id=plan_id,
        files=edits,
        diff=diff,
        rendered_diff=rendered_diff,
        base_revision=base_revision,
        source_revision=source_revision,
        renderer=renderer,
        validation=validation,
        warnings=warnings,
        digest=digest,
    )


def _plan_digest(
    *,
    plan_id: str,
    action: ActionPlanV3,
    source: WorkloadSource,
    edits: Sequence[FileEdit],
    diff: str,
    rendered_diff: str,
    base_revision: str,
    source_revision: str | None,
    validation: ValidationResult,
    warnings: list[str],
    renderer: RendererKind,
) -> str:
    payload = {
        "plan_id": plan_id,
        "action": action.model_dump(mode="json"),
        "binding": {
            "id": source.id,
            "provider_id": source.provider_id,
            "repo_url": source.repo_url,
            "ref": source.ref,
            "path": source.path,
            "namespace": source.namespace,
            "workload_kind": source.workload_kind,
            "workload_name": source.workload_name,
            "renderer": source.renderer.value,
            "status": source.status.value,
            "revision": source.revision,
            "source_revision": source.source_revision,
        },
        "files": [edit.model_dump(mode="json") for edit in edits],
        "diff": diff,
        "rendered_diff": rendered_diff,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "renderer": renderer.value,
        "validation": validation.model_dump(mode="json"),
        "warnings": warnings,
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HelmPlanError("plan inputs must be deterministically JSON-serializable") from exc
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _validate_flux_manifest_owner(source: WorkloadSource, base_revision: str) -> Mapping[str, Any]:
    config = source.renderer_config
    manifest_source = _mapping(config.get("manifest_source"))
    if manifest_source is None:
        raise FluxHelmReleaseUnsupportedError("HelmRelease manifest is not explicitly owned by Git")
    if (
        manifest_source.get("provider_id") != source.provider_id
        or manifest_source.get("repo_url") != source.repo_url
        or manifest_source.get("ref") != source.ref
    ):
        raise FluxHelmReleaseUnsupportedError(
            "HelmRelease manifest owner does not match the resolved repository binding"
        )
    owner_path = _canonical_path(manifest_source.get("path"), allow_root=True, error=FluxHelmReleaseUnsupportedError)
    source_path = _canonical_path(source.path, allow_root=True, error=FluxHelmReleaseUnsupportedError)
    if owner_path != source_path:
        raise FluxHelmReleaseUnsupportedError("HelmRelease manifest owner path does not match the resolved source path")
    revision = manifest_source.get("revision")
    if revision is not None and (
        not _revision_matches_snapshot(revision, source.revision or "")
        or not _revision_matches_snapshot(revision, base_revision)
    ):
        raise FluxHelmReleaseUnsupportedError(
            "HelmRelease manifest owner revision must match the immutable repository snapshot"
        )
    return manifest_source


def _find_release_manifest(
    root: Path,
    snapshot_files: Mapping[str, Any],
    source: WorkloadSource,
    owner: Mapping[str, Any],
) -> _ReleaseManifest:
    scope_path = _canonical_path(owner.get("path"), allow_root=True, error=FluxHelmReleaseUnsupportedError)
    candidates: list[_ReleaseManifest] = []
    for path in sorted(snapshot_files):
        if not _is_in_scope(path, scope_path) or PurePosixPath(path).suffix.lower() not in {
            ".yaml",
            ".yml",
            ".json",
        }:
            continue
        content = _read_source_file(root, snapshot_files, path, FluxHelmReleasePlanError)
        if PurePosixPath(path).suffix.lower() == ".json":
            if _HELMRELEASE_JSON_KIND.search(content) is None:
                continue
        elif _HELMRELEASE_KIND.search(content) is None:
            continue
        parsed = _parse_release_content(path, content, FluxHelmReleasePlanError)
        for index, document in enumerate(parsed):
            if not isinstance(document, Mapping):
                continue
            metadata = _mapping(document.get("metadata"))
            api_version = document.get("apiVersion")
            if (
                document.get("kind") == "HelmRelease"
                and isinstance(api_version, str)
                and api_version.startswith("helm.toolkit.fluxcd.io/")
                and metadata is not None
                and metadata.get("name") == source.controller_name
                and metadata.get("namespace") == source.controller_namespace
            ):
                candidates.append(
                    _ReleaseManifest(
                        path=path,
                        content=content,
                        documents=parsed,
                        document_index=index,
                        yaml=_roundtrip_yaml(content) if PurePosixPath(path).suffix.lower() != ".json" else None,
                        is_json=PurePosixPath(path).suffix.lower() == ".json",
                    )
                )
    if not candidates:
        raise FluxHelmReleaseUnsupportedError(
            "an exact Git-owned HelmRelease manifest was not found in the source path"
        )
    if len(candidates) != 1:
        raise FluxHelmReleaseUnsupportedError("multiple Git-owned HelmRelease manifests match the controller identity")
    return candidates[0]


def _flux_chart_context(
    root: Path,
    snapshot_files: Mapping[str, Any],
    source: WorkloadSource,
    spec: Mapping[str, Any],
    base_revision: str,
) -> tuple[str, str, str, tuple[str, ...], bool]:
    renderer_config = _mapping(source.renderer_config)
    if renderer_config is None:
        raise FluxHelmReleaseUnsupportedError("Flux renderer configuration must be a mapping")
    configured_chart = _mapping(renderer_config.get("chart"))
    if configured_chart is None:
        raise FluxHelmReleaseUnsupportedError("Flux renderer configuration is missing chart ownership metadata")

    if "chartRef" in spec:
        chart_ref = _mapping(spec.get("chartRef"))
        if chart_ref is None:
            raise FluxHelmReleaseUnsupportedError("HelmRelease spec.chartRef must be a mapping")
        chart_ref_kind = text(chart_ref.get("kind")) or "HelmChart"
        chart_ref_name = text(chart_ref.get("name"))
        chart_ref_namespace = text(chart_ref.get("namespace")) or source.controller_namespace
        discovered_chart_ref = _mapping(renderer_config.get("chart_ref"))
        if (
            discovered_chart_ref is None
            or discovered_chart_ref.get("kind") != chart_ref_kind
            or discovered_chart_ref.get("name") != chart_ref_name
            or discovered_chart_ref.get("namespace") != chart_ref_namespace
        ):
            raise FluxHelmReleaseUnsupportedError("Flux chartRef changed after source discovery")
        if chart_ref_kind != "HelmChart":
            raise FluxHelmReleaseUnsupportedError(
                f"remote-chart-not-in-snapshot: chartRef kind {chart_ref_kind!r} is not a local HelmChart"
            )
        source_ref = _mapping(renderer_config.get("chart_source_ref"))
        if source_ref is None:
            raise FluxHelmReleaseUnsupportedError("Flux HelmChart source identity was not captured during discovery")
        configured_version = configured_chart.get("version")
        raw_values_files: Any = renderer_config.get("chart_values_files", [])
        ignore_missing_values_files: Any = renderer_config.get("chart_ignore_missing_values_files", False)
    else:
        raw_chart = _mapping(spec.get("chart"))
        chart_spec = _mapping(raw_chart.get("spec")) if raw_chart is not None else None
        if chart_spec is None:
            raise FluxHelmReleaseUnsupportedError("HelmRelease spec.chart.spec must be a mapping")
        source_ref = _mapping(chart_spec.get("sourceRef"))
        if source_ref is None or (text(source_ref.get("kind")) or "HelmRepository") != "GitRepository":
            raise FluxHelmReleaseUnsupportedError(
                "remote-chart-not-in-snapshot: HelmRepository/OCIRepository chart artifacts are not in the Git snapshot"
            )
        configured_source_ref = _mapping(renderer_config.get("chart_source_ref"))
        if configured_source_ref is not None and (
            configured_source_ref.get("kind") != "GitRepository"
            or configured_source_ref.get("name") != text(source_ref.get("name"))
            or configured_source_ref.get("namespace")
            != (text(source_ref.get("namespace")) or source.controller_namespace)
        ):
            raise FluxHelmReleaseUnsupportedError("Flux chart source reference changed after source discovery")
        configured_version = chart_spec.get("version")
        raw_values_files = chart_spec.get("valuesFiles", [])
        ignore_missing_values_files = chart_spec.get("ignoreMissingValuesFiles", False)

    if (text(source_ref.get("kind")) or "HelmRepository") != "GitRepository":
        raise FluxHelmReleaseUnsupportedError("remote-chart-not-in-snapshot: chart source is not a GitRepository")
    raw_chart_path = configured_chart.get("name")
    if not isinstance(raw_chart_path, str):
        raise FluxHelmReleaseUnsupportedError("Flux Git chart path must be explicit")
    chart_path = _canonical_path(
        raw_chart_path.removeprefix("./"),
        allow_root=True,
        error=FluxHelmReleaseUnsupportedError,
    )
    if configured_chart.get("source_kind") != "GitRepository":
        raise FluxHelmReleaseUnsupportedError("Flux Helm chart source is not a GitRepository")
    configured_repo_url = configured_chart.get("repo_url")
    if not isinstance(configured_repo_url, str) or canonical_repository_identity(
        configured_repo_url
    ) != canonical_repository_identity(source.repo_url):
        raise FluxHelmReleaseUnsupportedError(
            "chart-source-not-in-snapshot: Flux Git chart belongs to another repository"
        )

    source_evidence = _mapping(source.evidence.get("source_ref"))
    if source_evidence is None:
        raise FluxHelmReleaseUnsupportedError("Flux chart source identity was not captured during discovery")
    expected_source_namespace = text(source_ref.get("namespace")) or source.controller_namespace
    if (
        source_evidence.get("kind") != source_ref.get("kind")
        or source_evidence.get("name") != source_ref.get("name")
        or (source_evidence.get("namespace") or source.controller_namespace) != expected_source_namespace
    ):
        raise FluxHelmReleaseUnsupportedError("Flux chart source reference changed after source discovery")
    source_revision = _revision_commit(source.source_revision) if source.source_revision else None
    if source_revision is None or source_revision != base_revision.lower():
        raise FluxHelmReleaseUnsupportedError(
            "Flux GitRepository artifact revision must match the immutable repository snapshot"
        )

    chart_metadata_path = _join_path(chart_path, "Chart.yaml")
    if chart_metadata_path not in snapshot_files:
        raise FluxHelmReleaseUnsupportedError("Flux local Git chart is unavailable in the repository snapshot")
    chart_metadata_content = _read_source_file(
        root,
        snapshot_files,
        chart_metadata_path,
        FluxHelmReleaseUnsupportedError,
    )
    chart_metadata, _, _ = _load_values(
        chart_metadata_content,
        chart_metadata_path,
        FluxHelmReleaseUnsupportedError,
        round_trip=False,
    )
    if not isinstance(chart_metadata, Mapping):
        raise FluxHelmReleaseUnsupportedError("Flux local chart metadata must be a mapping")
    _reject_chart_dependencies(chart_metadata, FluxHelmReleaseUnsupportedError)
    actual_chart_version = chart_metadata.get("version")
    if not isinstance(actual_chart_version, str) or not actual_chart_version.strip():
        raise FluxHelmReleaseUnsupportedError("Flux local Git chart version is missing")
    if not _local_chart_version_selector_matches(configured_version, actual_chart_version):
        raise FluxHelmReleaseUnsupportedError("Flux local chart version selector cannot be resolved exactly")
    if not _local_chart_version_selector_matches(configured_chart.get("version"), actual_chart_version):
        raise FluxHelmReleaseUnsupportedError("Flux chart version changed after source discovery")

    chart_values_files = _normalize_flux_chart_values_files(raw_values_files)
    discovered_values_files = _normalize_flux_chart_values_files(renderer_config.get("chart_values_files", []))
    if chart_values_files != discovered_values_files:
        raise FluxHelmReleaseUnsupportedError("Flux chart.spec.valuesFiles changed after source discovery")
    if type(ignore_missing_values_files) is not bool:
        raise FluxHelmReleaseUnsupportedError("Flux ignoreMissingValuesFiles must be a boolean")
    discovered_ignore_missing = renderer_config.get("chart_ignore_missing_values_files", False)
    if type(discovered_ignore_missing) is not bool or discovered_ignore_missing != ignore_missing_values_files:
        raise FluxHelmReleaseUnsupportedError("Flux ignoreMissingValuesFiles changed after source discovery")

    release_name = flux_release_name(source.controller_name, spec)
    release_namespace = spec.get("targetNamespace") or source.controller_namespace
    if len(release_name) > 53:
        raise FluxHelmReleaseUnsupportedError(
            "Flux-computed release name exceeds Helm's 53-character limit; Flux does not truncate it"
        )
    configured_release_name = renderer_config.get("release_name")
    configured_release_namespace = renderer_config.get("release_namespace")
    if configured_release_name not in (None, release_name):
        raise FluxHelmReleaseUnsupportedError("Flux release name changed after source discovery")
    if configured_release_namespace not in (None, release_namespace):
        raise FluxHelmReleaseUnsupportedError("Flux release namespace changed after source discovery")
    if release_namespace != source.namespace:
        raise FluxHelmReleaseUnsupportedError("Flux HelmRelease target namespace does not match the workload binding")
    if not isinstance(release_name, str) or not release_name.strip():
        raise FluxHelmReleaseUnsupportedError("Flux HelmRelease releaseName must be explicit")
    if not isinstance(release_namespace, str) or not release_namespace.strip():
        raise FluxHelmReleaseUnsupportedError("Flux HelmRelease targetNamespace must be explicit")
    return chart_path, release_name, release_namespace, chart_values_files, ignore_missing_values_files


def _normalize_flux_chart_values_files(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise FluxHelmReleaseUnsupportedError("Flux chart.spec.valuesFiles must be a list of snapshot-relative paths")
    if len(value) > _MAX_FLUX_VALUES_FILES:
        raise FluxHelmReleaseUnsupportedError("Flux chart.spec.valuesFiles exceeds the supported file-count limit")
    return tuple(_canonical_path(path, allow_root=False, error=FluxHelmReleaseUnsupportedError) for path in value)


def _load_flux_chart_values_files(
    root: Path,
    snapshot_files: Mapping[str, Any],
    paths: Sequence[str],
    *,
    ignore_missing: bool,
) -> dict[str, Any]:
    if len(paths) > _MAX_FLUX_VALUES_FILES:
        raise FluxHelmReleaseUnsupportedError("Flux chart.spec.valuesFiles exceeds the supported file-count limit")
    merged: dict[str, Any] = {}
    total_bytes = 0
    for raw_path in paths:
        path = _canonical_path(raw_path, allow_root=False, error=FluxHelmReleaseUnsupportedError)
        if path not in snapshot_files:
            if ignore_missing:
                continue
            raise FluxHelmReleaseUnsupportedError(
                f"Flux chart.spec.valuesFiles path {path!r} is not present in the repository snapshot"
            )
        content = _read_source_file(root, snapshot_files, path, FluxHelmReleaseUnsupportedError)
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > _MAX_FLUX_VALUES_BYTES:
            raise FluxHelmReleaseUnsupportedError("Flux chart.spec.valuesFiles exceeds the supported size limit")
        values, _, _ = _load_values(
            content,
            path,
            FluxHelmReleaseUnsupportedError,
            round_trip=False,
        )
        if not isinstance(values, Mapping):
            raise FluxHelmReleaseUnsupportedError(
                f"Flux chart.spec.valuesFiles entry {path!r} must contain a YAML mapping"
            )
        merged = _merge_flux_value_maps(merged, _plain_flux_values(values))
    return merged


def _load_flux_values_from(
    root: Path,
    snapshot_files: Mapping[str, Any],
    source: WorkloadSource,
    spec: Mapping[str, Any],
    base_revision: str,
) -> dict[str, Any]:
    raw_values_from = spec.get("valuesFrom", [])
    if raw_values_from is None:
        raw_values_from = []
    if not isinstance(raw_values_from, list):
        raise FluxHelmReleaseUnsupportedError("HelmRelease spec.valuesFrom must be a list")
    if not raw_values_from:
        return {}
    if len(raw_values_from) > _MAX_FLUX_VALUES_FILES:
        raise FluxHelmReleaseUnsupportedError("HelmRelease spec.valuesFrom exceeds the supported reference-count limit")

    config = _mapping(source.renderer_config)
    discovered_refs = config.get("values_from") if config is not None else None
    if not isinstance(discovered_refs, list) or len(discovered_refs) != len(raw_values_from):
        raise FluxHelmReleaseUnsupportedError(
            "Flux valuesFrom ownership was not captured for every reference during discovery"
        )

    merged: dict[str, Any] = {}
    total_bytes = 0
    release_namespace = source.controller_namespace
    for index, (raw_reference, raw_discovered) in enumerate(zip(raw_values_from, discovered_refs, strict=True)):
        reference = _mapping(raw_reference)
        discovered = _mapping(raw_discovered)
        if reference is None or discovered is None:
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom references must be mappings")
        kind = text(reference.get("kind"))
        name = text(reference.get("name"))
        raw_namespace = reference.get("namespace")
        if raw_namespace is not None and (not isinstance(raw_namespace, str) or raw_namespace != raw_namespace.strip()):
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom namespace must be a string")
        namespace = text(raw_namespace) or release_namespace
        if namespace != release_namespace:
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom references must remain in the HelmRelease namespace")
        if kind != discovered.get("kind") or name != discovered.get("name") or namespace != discovered.get("namespace"):
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom references changed after source discovery")
        raw_values_key = reference.get("valuesKey")
        if raw_values_key is not None and (
            not isinstance(raw_values_key, str)
            or not raw_values_key.strip()
            or raw_values_key != raw_values_key.strip()
        ):
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom valuesKey must be a non-empty string")
        values_key = text(raw_values_key) or "values.yaml"
        optional = reference.get("optional", False)
        if type(optional) is not bool:
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom optional must be a boolean")
        if (
            discovered.get("values_key") != text(raw_values_key)
            or discovered.get("optional") is not optional
            or discovered.get("target_path") is not None
        ):
            raise FluxHelmReleaseUnsupportedError("Flux valuesFrom merge options changed after source discovery")
        if discovered.get("git_managed") is not True:
            raise FluxHelmReleaseUnsupportedError(
                "Flux valuesFrom source is not uniquely managed by a GitRepository-backed Kustomization"
            )
        if kind not in {"ConfigMap", "Secret"}:
            raise FluxHelmReleaseUnsupportedError(f"unsupported Flux valuesFrom kind: {kind or 'unknown'}")

        target_path = reference.get("targetPath")
        if target_path not in (None, ""):
            raise FluxHelmReleaseUnsupportedError(
                "unsupported-values-from-target-path: Flux targetPath value merging is not implemented"
            )

        repo_url = text(discovered.get("repo_url"))
        ref = text(discovered.get("ref"))
        if (
            repo_url is None
            or canonical_repository_identity(repo_url) != canonical_repository_identity(source.repo_url)
            or ref != source.ref
        ):
            raise FluxHelmReleaseUnsupportedError(
                "valuesFrom source is not present in the HelmRelease repository snapshot"
            )
        if not _revision_matches_snapshot(discovered.get("revision"), base_revision):
            raise FluxHelmReleaseUnsupportedError(
                "valuesFrom source revision must match the immutable repository snapshot"
            )
        path = _canonical_path(
            discovered.get("path"),
            allow_root=True,
            error=FluxHelmReleaseUnsupportedError,
        )
        target_namespace = discovered.get("target_namespace")
        if target_namespace is not None and (not isinstance(target_namespace, str) or target_namespace != namespace):
            raise FluxHelmReleaseUnsupportedError(
                "valuesFrom Kustomization targetNamespace does not match the HelmRelease namespace"
            )
        document = _find_flux_values_manifest(
            root,
            snapshot_files,
            path,
            kind=kind,
            name=name or "",
            namespace=namespace,
            target_namespace=target_namespace,
        )
        value = _flux_values_from_document(
            document,
            kind=kind,
            name=name or "",
            values_key=values_key,
            optional=optional,
        )
        if value is not None:
            total_bytes += len(value.encode("utf-8"))
            if total_bytes > _MAX_FLUX_VALUES_BYTES:
                raise FluxHelmReleaseUnsupportedError("Flux valuesFrom exceeds the supported size limit")
            parsed, _, _ = _load_values(
                value,
                f"valuesFrom/{kind.lower()}-{name}.yaml",
                FluxHelmReleaseUnsupportedError,
                round_trip=False,
            )
            if not isinstance(parsed, Mapping):
                raise FluxHelmReleaseUnsupportedError(
                    f"Flux valuesFrom {kind}/{name} valuesKey must contain a YAML mapping"
                )
            merged = _merge_flux_value_maps(merged, _plain_flux_values(parsed))
    return merged


def _find_flux_values_manifest(
    root: Path,
    snapshot_files: Mapping[str, Any],
    scope: str,
    *,
    kind: str,
    name: str,
    namespace: str,
    target_namespace: Any,
) -> Mapping[str, Any]:
    if scope in snapshot_files:
        candidate_paths = [scope]
    else:
        candidate_paths = [
            path
            for path in sorted(snapshot_files)
            if _is_in_scope(path, scope) and PurePosixPath(path).suffix.lower() in {".yaml", ".yml", ".json"}
        ]
    matches: list[Mapping[str, Any]] = []
    for path in candidate_paths:
        if PurePosixPath(path).suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        content = _read_source_file(root, snapshot_files, path, FluxHelmReleaseUnsupportedError)
        for document in _parse_release_content(path, content, FluxHelmReleaseUnsupportedError):
            if not isinstance(document, Mapping) or document.get("kind") != kind:
                continue
            metadata = _mapping(document.get("metadata"))
            if metadata is None or metadata.get("name") != name:
                continue
            manifest_namespace = text(metadata.get("namespace"))
            if manifest_namespace is None:
                manifest_namespace = text(target_namespace)
            if manifest_namespace is None:
                raise FluxHelmReleaseUnsupportedError(
                    f"Flux valuesFrom {kind}/{name} has no explicit or Kustomization target namespace"
                )
            if manifest_namespace == namespace:
                matches.append(document)
    if not matches:
        raise FluxHelmReleaseUnsupportedError(
            f"Flux valuesFrom {kind}/{name} manifest is not present in the immutable repository snapshot"
        )
    if len(matches) != 1:
        raise FluxHelmReleaseUnsupportedError(f"multiple Git manifests match Flux valuesFrom {kind}/{name}")
    return matches[0]


def _flux_values_from_document(
    document: Mapping[str, Any],
    *,
    kind: str,
    name: str,
    values_key: str,
    optional: bool,
) -> str | None:
    if document.get("apiVersion") != "v1":
        raise FluxHelmReleaseUnsupportedError(f"Flux valuesFrom {kind}/{name} must be a core v1 resource")
    if kind == "ConfigMap":
        data = _mapping(document.get("data")) or {}
        value = data.get(values_key)
        if value is None:
            if optional:
                return None
            raise FluxHelmReleaseUnsupportedError(
                f"Flux valuesFrom ConfigMap/{name} does not contain valuesKey {values_key!r}"
            )
        if not isinstance(value, str):
            raise FluxHelmReleaseUnsupportedError(f"Flux valuesFrom ConfigMap/{name} valuesKey must contain text")
        return value

    data = _mapping(document.get("data")) or {}
    string_data = _mapping(document.get("stringData")) or {}
    if values_key in string_data:
        value = string_data[values_key]
        if not isinstance(value, str):
            raise FluxHelmReleaseUnsupportedError(
                f"Flux valuesFrom Secret/{name} stringData valuesKey must contain text"
            )
        return value
    encoded = data.get(values_key)
    if encoded is None:
        if optional:
            return None
        raise FluxHelmReleaseUnsupportedError(
            f"Flux valuesFrom Secret/{name} does not contain valuesKey {values_key!r}"
        )
    if not isinstance(encoded, str):
        raise FluxHelmReleaseUnsupportedError(f"Flux valuesFrom Secret/{name} data valuesKey must be base64 text")
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise FluxHelmReleaseUnsupportedError(
            f"Flux valuesFrom Secret/{name} data valuesKey is not valid base64-encoded UTF-8"
        ) from exc


def _plain_flux_values(value: Mapping[str, Any]) -> dict[str, Any]:
    def convert(item: Any, active: set[int], depth: int) -> Any:
        if depth > 64:
            raise FluxHelmReleaseUnsupportedError("Flux Helm values nesting exceeds the safe limit")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise FluxHelmReleaseUnsupportedError("recursive aliases are unsupported in Flux Helm values")
            active.add(identity)
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise FluxHelmReleaseUnsupportedError("Flux Helm values mapping keys must be strings")
                result[key] = convert(child, active, depth + 1)
            active.remove(identity)
            return result
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                raise FluxHelmReleaseUnsupportedError("recursive aliases are unsupported in Flux Helm values")
            active.add(identity)
            sequence_result = [convert(child, active, depth + 1) for child in item]
            active.remove(identity)
            return sequence_result
        if item is None or type(item) in {str, int, float, bool}:
            return item
        raise FluxHelmReleaseUnsupportedError("Flux Helm values contain an unsupported YAML value type")

    result = convert(value, set(), 0)
    if not isinstance(result, dict):
        raise FluxHelmReleaseUnsupportedError("Flux Helm values must contain a YAML mapping")
    return result


def _merge_flux_value_maps(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(left))
    for key, value in right.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_flux_value_maps(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _local_chart_version_selector_matches(selector: Any, chart_version: str) -> bool:
    return selector in (None, "", "*") or (isinstance(selector, str) and selector == chart_version)


def _parse_release_content(path: str, content: str, error_type: type[HelmPlanError]) -> list[Any]:
    if PurePosixPath(path).suffix.lower() == ".json":
        try:
            return [json.loads(content, object_pairs_hook=_unique_json_object)]
        except (json.JSONDecodeError, _DuplicateJSONKey, TypeError, ValueError) as exc:
            raise error_type(f"{path}: HelmRelease JSON could not be parsed") from exc
    yaml = _roundtrip_yaml(content)
    try:
        return list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise error_type(f"{path}: HelmRelease YAML could not be parsed") from exc


def _roundtrip_yaml(content: str) -> YAML:
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    if "\r\n" in content and "\n" not in content.replace("\r\n", ""):
        yaml.line_break = "\r\n"
    return yaml


def _manifest_from_documents(
    path: str,
    documents: list[Any],
    document_index: int,
) -> MutableMapping[str, Any]:
    if document_index >= len(documents) or not isinstance(documents[document_index], MutableMapping):
        raise FluxHelmReleasePlanError(f"{path}: HelmRelease document index is unavailable")
    return documents[document_index]


def _managed_postrenderer_patches(
    spec: Mapping[str, Any],
    error_type: type[HelmPlanError],
) -> list[_ManagedPostRendererPatch]:
    raw_renderers = spec.get("postRenderers", [])
    if raw_renderers is None:
        raw_renderers = []
    if not isinstance(raw_renderers, list):
        raise error_type("HelmRelease spec.postRenderers must be a list")
    patches: list[_ManagedPostRendererPatch] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry_index, renderer in enumerate(raw_renderers):
        if not isinstance(renderer, Mapping) or set(renderer) != {"kustomize"}:
            raise error_type("only GreenKube-managed Kustomize post-renderers can be render-validated")
        kustomize = _mapping(renderer.get("kustomize"))
        if kustomize is None or set(kustomize) != {"patches"}:
            raise error_type("only strategic-merge Kustomize post-renderer patches are supported")
        raw_patches = kustomize.get("patches")
        if not isinstance(raw_patches, list):
            raise error_type("Kustomize post-renderer patches must be a list")
        for patch_index, entry in enumerate(raw_patches):
            if not isinstance(entry, Mapping) or set(entry) != {"target", "patch"}:
                raise error_type("post-renderer patches must use an explicit target and inline strategic-merge patch")
            selector = _mapping(entry.get("target"))
            patch_text = entry.get("patch")
            if selector is None or not isinstance(patch_text, str):
                raise error_type("post-renderer patch target and patch text must be explicit")
            patch = _parse_managed_patch(patch_text, error_type)
            patch_target = _patch_target(patch, error_type)
            expected_selector = _target_selector(patch_target)
            selector_values = dict(selector)
            if selector_values != expected_selector and selector_values != _legacy_target_selector(patch_target):
                raise error_type("managed post-renderer target selector does not match its patch identity")
            identity = _target_identity(patch_target)
            if identity in seen:
                raise error_type("multiple managed post-renderer patches target the same workload")
            seen.add(identity)
            patches.append(
                _ManagedPostRendererPatch(
                    target=patch_target,
                    document=patch,
                    entry_index=entry_index,
                    patch_index=patch_index,
                )
            )
    return patches


def _parse_managed_patch(
    content: str,
    error_type: type[HelmPlanError],
) -> MutableMapping[str, Any]:
    yaml = _roundtrip_yaml(content)
    try:
        documents = list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise error_type("managed Flux post-renderer patch is invalid YAML") from exc
    if len(documents) != 1 or not isinstance(documents[0], MutableMapping):
        raise error_type("managed Flux post-renderer patch must be one resource mapping")
    patch = documents[0]
    metadata = _mapping(patch.get("metadata"))
    annotations = _mapping(metadata.get("annotations")) if metadata is not None else None
    if annotations is None or annotations.get(_MANAGED_ANNOTATION) != "true":
        raise error_type("Flux post-renderer patch is not marked as GreenKube-managed")
    if set(patch) - {"apiVersion", "kind", "metadata", "spec"}:
        raise error_type("managed Flux post-renderer patch contains unsupported resource fields")
    if metadata is None or set(metadata) - {"name", "namespace", "annotations"}:
        raise error_type("managed Flux post-renderer patch metadata is unsupported")
    if set(annotations) - {_MANAGED_ANNOTATION, _ACTION_ID_ANNOTATION}:
        raise error_type("managed Flux post-renderer patch annotations are unsupported")
    if _ACTION_ID_ANNOTATION in annotations and not isinstance(annotations[_ACTION_ID_ANNOTATION], str):
        raise error_type("managed Flux post-renderer action ID annotation must be a string")
    target = _patch_target(patch, error_type)
    pod_path = _POD_SPEC_PATHS.get(target.kind)
    if pod_path is None:
        raise error_type(f"managed post-renderer workload kind {target.kind!r} is unsupported")
    current: Any = patch.get("spec")
    for field in pod_path[1:]:
        if not isinstance(current, Mapping) or set(current) != {field}:
            raise error_type("managed Flux post-renderer patch must contain container resources only")
        current = current[field]
    if not isinstance(current, Mapping) or set(current) != {"containers"}:
        raise error_type("managed Flux post-renderer patch must contain container resources only")
    containers = current.get("containers")
    if not isinstance(containers, list) or not containers:
        raise error_type("managed Flux post-renderer patch requires a non-empty containers list")
    seen_containers: set[str] = set()
    for container in containers:
        if not isinstance(container, Mapping) or set(container) != {"name", "resources"}:
            raise error_type("managed Flux post-renderer containers may contain resources only")
        name = container.get("name")
        resources = _mapping(container.get("resources"))
        if not isinstance(name, str) or not name.strip() or name in seen_containers or resources is None:
            raise error_type("managed Flux post-renderer container names and resources must be unique mappings")
        if set(resources) - {"requests", "limits"}:
            raise error_type("managed Flux post-renderer resources may contain requests and limits only")
        if not resources:
            raise error_type("managed Flux post-renderer container resources must not be empty")
        for field_name in ("requests", "limits"):
            field_values = _mapping(resources.get(field_name))
            if field_name in resources and field_values is None:
                raise error_type(f"managed Flux post-renderer resources.{field_name} must be a mapping")
        seen_containers.add(name)
    return patch


def _patch_target(patch: Mapping[str, Any], error_type: type[HelmPlanError]) -> ResourceTarget:
    metadata = _mapping(patch.get("metadata"))
    if metadata is None:
        raise error_type("managed Flux post-renderer patch metadata is required")
    api_version = patch.get("apiVersion")
    kind = patch.get("kind")
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(api_version, str) or not api_version:
        raise error_type("managed Flux post-renderer patch requires apiVersion, kind, and name")
    if not isinstance(kind, str) or not kind:
        raise error_type("managed Flux post-renderer patch requires apiVersion, kind, and name")
    if not isinstance(name, str) or not name:
        raise error_type("managed Flux post-renderer patch requires apiVersion, kind, and name")
    if not isinstance(namespace, str):
        raise error_type("managed Flux post-renderer patch requires an exact namespace")
    try:
        return ResourceTarget(api_version=api_version, kind=kind, name=name, namespace=namespace)
    except ValidationError as exc:
        raise error_type("managed Flux post-renderer patch has an invalid resource identity") from exc


def _target_selector(target: ResourceTarget) -> dict[str, str]:
    group, separator, version = target.api_version.partition("/")
    if not separator:
        group, version = "", group
    return {
        "group": re.escape(group) if group else "^$",
        "version": re.escape(version),
        "kind": re.escape(target.kind),
        "name": re.escape(target.name),
        "namespace": re.escape(target.namespace),
    }


def _legacy_target_selector(target: ResourceTarget) -> dict[str, str]:
    group, separator, version = target.api_version.partition("/")
    if not separator:
        group, version = "", group
    return {
        "group": group,
        "version": version,
        "kind": target.kind,
        "name": target.name,
        "namespace": target.namespace,
    }


def _apply_managed_postrenderer_patches(
    result: RawRenderResult,
    patches: Sequence[_ManagedPostRendererPatch],
    error_type: type[HelmPlanError],
) -> RawRenderResult:
    identities = {_target_identity(resource.target): resource for resource in result.resources}
    if len(identities) != len(result.resources):
        raise error_type("Helm chart render contains duplicate resource identities")
    manifests = {identity: copy.deepcopy(resource.manifest) for identity, resource in identities.items()}
    for item in patches:
        identity = _target_identity(item.target)
        manifest = manifests.get(identity)
        if manifest is None:
            raise error_type("managed Flux post-renderer target is absent from the Helm chart render")
        patch = item.document
        metadata = _mapping(patch.get("metadata"))
        annotations = _mapping(metadata.get("annotations")) if metadata is not None else None
        if annotations is None:
            raise error_type("managed Flux post-renderer annotations are unavailable")
        manifest_metadata = manifest.get("metadata")
        if not isinstance(manifest_metadata, MutableMapping):
            raise error_type("rendered workload metadata must be a mapping")
        target_annotations = manifest_metadata.get("annotations")
        if target_annotations is None:
            target_annotations = CommentedMap()
            manifest_metadata["annotations"] = target_annotations
        elif not isinstance(target_annotations, MutableMapping):
            raise error_type("rendered workload annotations must be a mapping")
        for key, value in annotations.items():
            target_annotations[key] = value
        operations = _operations_from_patch(patch, item.target, error_type)
        try:
            _apply_operations(manifest, item.target.kind, operations)
            _validate_operation_resources(manifest, item.target.kind, operations)
        except RawManifestPlanError as exc:
            raise error_type(f"managed Flux post-renderer patch failed validation: {exc}") from exc
    return _normalise_rendered_result(result, manifests, error_type)


def _operations_from_patch(
    patch: Mapping[str, Any],
    target: ResourceTarget,
    error_type: type[HelmPlanError],
) -> list[SetContainerResources]:
    current: Any = patch.get("spec")
    for field in _POD_SPEC_PATHS[target.kind][1:]:
        if not isinstance(current, Mapping):
            raise error_type("managed Flux post-renderer pod specification is invalid")
        current = current.get(field)
    containers = current.get("containers") if isinstance(current, Mapping) else None
    if not isinstance(containers, list):
        raise error_type("managed Flux post-renderer containers are invalid")
    operations: list[SetContainerResources] = []
    try:
        for container in containers:
            resources = _mapping(container.get("resources"))
            if resources is None:
                raise error_type("managed Flux post-renderer container resources are invalid")
            operations.append(
                SetContainerResources(
                    container=container["name"],
                    resources=ContainerResources(
                        requests=dict(resources.get("requests", {})),
                        limits=dict(resources.get("limits", {})),
                    ),
                )
            )
    except (ValidationError, TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, HelmPlanError):
            raise
        raise error_type("managed Flux post-renderer quantities are invalid") from exc
    return operations


def _normalise_rendered_result(
    original: RawRenderResult,
    manifests: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
    error_type: type[HelmPlanError],
) -> RawRenderResult:
    stream = io.StringIO()
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False
    try:
        yaml.dump_all(
            [_plain(manifests[_target_identity(resource.target)]) for resource in original.resources],
            stream,
        )
        normalized = render_raw({"rendered/helm-release.yaml": stream.getvalue()})
    except (YAMLError, RawRenderError, RecursionError, TypeError, ValueError) as exc:
        raise error_type("Flux post-renderer output could not be normalized") from exc
    source_paths = {_target_identity(resource.target): resource.source_path for resource in original.resources}
    resources = tuple(
        replace(resource, source_path=source_paths[_target_identity(resource.target)])
        for resource in normalized.resources
    )
    return replace(
        normalized,
        renderer=RendererKind.HELM_RELEASE,
        resources=resources,
        warnings=tuple(sorted(set(original.warnings + normalized.warnings))),
    )


def _update_release_postrenderer(
    release: _ReleaseManifest,
    action: ActionPlanV3,
    operations: Sequence[SetContainerResources],
    existing_patches: Sequence[_ManagedPostRendererPatch],
    *,
    container_order: Sequence[str] = (),
) -> str:
    manifest = release.document
    raw_spec = manifest.get("spec")
    if not isinstance(raw_spec, MutableMapping):
        raise FluxHelmReleasePlanError("Git-owned HelmRelease spec must be a mapping")
    spec = raw_spec
    matching = [item for item in existing_patches if item.target == action.target]
    if len(matching) > 1:
        raise FluxHelmReleasePlanError("multiple GreenKube post-renderer patches target this workload")
    if matching:
        existing = matching[0]
        renderer_list = spec.get("postRenderers")
        if not isinstance(renderer_list, list) or existing.entry_index >= len(renderer_list):
            raise FluxHelmReleasePlanError("managed Flux post-renderer list changed unexpectedly")
        entry = renderer_list[existing.entry_index]
        kustomize = _mapping(entry.get("kustomize")) if isinstance(entry, Mapping) else None
        patch_entries = kustomize.get("patches") if kustomize is not None else None
        if not isinstance(patch_entries, list) or existing.patch_index >= len(patch_entries):
            raise FluxHelmReleasePlanError("managed Flux post-renderer patch changed unexpectedly")
        patch_doc = copy.deepcopy(existing.document)
        _merge_action_into_patch(patch_doc, action, operations, container_order=container_order)
        patch_text = _dump_patch(patch_doc)
        patch_entries[existing.patch_index]["target"] = CommentedMap(_target_selector(action.target))
        patch_entries[existing.patch_index]["patch"] = (
            patch_text if release.is_json else LiteralScalarString(patch_text)
        )
    else:
        raw_renderers = spec.get("postRenderers")
        if raw_renderers is None:
            raw_renderers = [] if release.is_json else CommentedSeq()
            spec["postRenderers"] = raw_renderers
        if not isinstance(raw_renderers, list):
            raise FluxHelmReleasePlanError("HelmRelease spec.postRenderers must be a list")
        patch_doc = _new_managed_patch(action, operations)
        patch_text = _dump_patch(patch_doc)
        if release.is_json:
            raw_renderers.append(
                {
                    "kustomize": {
                        "patches": [
                            {
                                "target": _target_selector(action.target),
                                "patch": patch_text,
                            }
                        ]
                    }
                }
            )
        else:
            raw_renderers.append(
                CommentedMap(
                    {
                        "kustomize": CommentedMap(
                            {
                                "patches": CommentedSeq(
                                    [
                                        CommentedMap(
                                            {
                                                "target": CommentedMap(_target_selector(action.target)),
                                                "patch": LiteralScalarString(patch_text),
                                            }
                                        )
                                    ]
                                )
                            }
                        )
                    }
                )
            )
    return _dump_release(release)


def _new_managed_patch(
    action: ActionPlanV3,
    operations: Sequence[SetContainerResources],
) -> MutableMapping[str, Any]:
    container_patches = [
        CommentedMap(
            {
                "name": operation.container,
                "resources": _resources_patch(operation.resources),
            }
        )
        for operation in operations
    ]
    patch = CommentedMap(
        {
            "apiVersion": action.target.api_version,
            "kind": action.target.kind,
            "metadata": CommentedMap(
                {
                    "name": action.target.name,
                    "namespace": action.target.namespace,
                    "annotations": CommentedMap(
                        {
                            _MANAGED_ANNOTATION: "true",
                            _ACTION_ID_ANNOTATION: action.action_id,
                        }
                    ),
                }
            ),
            "spec": _nested_pod_spec(action.target.kind, container_patches),
        }
    )
    return patch


def _resources_patch(resources: ContainerResources) -> MutableMapping[str, Any]:
    result: MutableMapping[str, Any] = CommentedMap()
    if resources.requests:
        result["requests"] = CommentedMap({name: resources.requests[name] for name in sorted(resources.requests)})
    if resources.limits:
        result["limits"] = CommentedMap({name: resources.limits[name] for name in sorted(resources.limits)})
    return result


def _nested_pod_spec(
    workload_kind: str,
    containers: list[MutableMapping[str, Any]],
) -> MutableMapping[str, Any]:
    path = _POD_SPEC_PATHS.get(workload_kind)
    if path is None:
        raise FluxHelmReleasePlanError(f"unsupported workload kind {workload_kind!r}")
    nested: MutableMapping[str, Any] = CommentedMap({"containers": CommentedSeq(containers)})
    for field in reversed(path[1:]):
        nested = CommentedMap({field: nested})
    return nested


def _merge_action_into_patch(
    patch: MutableMapping[str, Any],
    action: ActionPlanV3,
    operations: Sequence[SetContainerResources],
    *,
    container_order: Sequence[str] = (),
) -> None:
    metadata = _mapping(patch.get("metadata"))
    if not isinstance(metadata, MutableMapping):
        raise FluxHelmReleasePlanError("managed Flux post-renderer patch metadata is invalid")
    annotations = metadata.get("annotations")
    if not isinstance(annotations, MutableMapping):
        raise FluxHelmReleasePlanError("managed Flux post-renderer patch annotations are missing")
    annotations[_MANAGED_ANNOTATION] = "true"
    annotations[_ACTION_ID_ANNOTATION] = action.action_id
    current: Any = patch.get("spec")
    for field in _POD_SPEC_PATHS[action.target.kind][1:]:
        if not isinstance(current, MutableMapping):
            raise FluxHelmReleasePlanError("managed Flux post-renderer pod specification is invalid")
        current = current[field]
    raw_containers = current.get("containers") if isinstance(current, Mapping) else None
    if not isinstance(raw_containers, list):
        raise FluxHelmReleasePlanError("managed Flux post-renderer containers are invalid")
    containers = {container["name"]: container for container in raw_containers}
    for operation in operations:
        container = containers.get(operation.container)
        if container is None:
            container = CommentedMap({"name": operation.container, "resources": CommentedMap()})
            raw_containers.append(container)
            containers[operation.container] = container
        resource_map = container.get("resources")
        if not isinstance(resource_map, MutableMapping):
            raise FluxHelmReleasePlanError("managed Flux post-renderer resources must be a mapping")
        for field_name, quantities in (
            ("requests", operation.resources.requests),
            ("limits", operation.resources.limits),
        ):
            if quantities:
                _merge_resource_section(resource_map, field_name, quantities, "managed post-renderer patch")
    positions = {name: index for index, name in enumerate(container_order)}
    raw_containers.sort(key=lambda item: (positions.get(item["name"], len(positions)), item["name"]))


def _dump_patch(patch: Mapping[str, Any]) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False
    yaml.allow_unicode = True
    yaml.width = 4096
    stream = io.StringIO()
    try:
        yaml.dump(_plain(dict(patch)), stream)
    except (YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise FluxHelmReleasePlanError("managed Flux post-renderer patch could not be serialized") from exc
    return stream.getvalue().rstrip("\r\n")


def _dump_release(release: _ReleaseManifest) -> str:
    if release.is_json:
        try:
            updated = json.dumps(release.documents[0], ensure_ascii=False, indent=2, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise FluxHelmReleasePlanError("Git-owned HelmRelease JSON could not be serialized") from exc
        return f"{updated}\n" if release.content.endswith(("\n", "\r")) else updated
    if release.yaml is None:
        raise FluxHelmReleasePlanError("Git-owned HelmRelease YAML serializer is unavailable")
    stream = io.StringIO()
    try:
        release.yaml.dump_all(release.documents, stream)
    except (YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise FluxHelmReleasePlanError("Git-owned HelmRelease YAML could not be serialized") from exc
    updated = stream.getvalue()
    if release.content.endswith(("\n", "\r")):
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = updated.rstrip("\r\n")
    return updated


def _validate_flux_rendered_change(
    before: RawRenderResult,
    after: RawRenderResult,
    target: ResourceTarget,
    workload_kind: str,
    operations: Sequence[SetContainerResources],
    action_id: str,
) -> None:
    before_resources = _resources_by_identity(before)
    after_resources = _resources_by_identity(after)
    target_identity = _target_identity(target)
    if target_identity not in before_resources or target_identity not in after_resources:
        raise FluxHelmReleasePlanError("post-render validation did not preserve the exact target workload")
    if before_resources.keys() != after_resources.keys():
        raise FluxHelmReleasePlanError("post-render validation found added or removed chart resources")
    for identity in before_resources:
        if identity != target_identity and before_resources[identity] != after_resources[identity]:
            raise FluxHelmReleasePlanError("post-render validation found an unrelated resource change")

    expected = copy.deepcopy(before_resources[target_identity])
    try:
        _apply_operations(expected, workload_kind, list(operations))
        _validate_operation_resources(expected, workload_kind, list(operations))
    except RawManifestPlanError as exc:
        raise FluxHelmReleasePlanError(f"post-render target validation failed: {exc}") from exc
    metadata = expected.get("metadata")
    if not isinstance(metadata, MutableMapping):
        raise FluxHelmReleasePlanError("post-render target metadata must be a mapping")
    annotations = metadata.get("annotations")
    if annotations is None:
        annotations = CommentedMap()
        metadata["annotations"] = annotations
    elif not isinstance(annotations, MutableMapping):
        raise FluxHelmReleasePlanError("post-render target annotations must be a mapping")
    annotations[_MANAGED_ANNOTATION] = "true"
    annotations[_ACTION_ID_ANNOTATION] = action_id
    if expected != after_resources[target_identity]:
        raise FluxHelmReleasePlanError("post-render validation found changes outside the target container resources")
    try:
        _validate_operation_resources(after_resources[target_identity], workload_kind, list(operations))
    except RawManifestPlanError as exc:
        raise FluxHelmReleasePlanError(f"post-render target resource quantities are invalid: {exc}") from exc


def _operation_resources(
    manifest: Mapping[str, Any],
    workload_kind: str,
    operations: Sequence[SetContainerResources],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        pod_spec = _pod_spec(manifest, workload_kind)
        containers = _containers(pod_spec)
        for operation in operations:
            container = _find_container(containers, operation.container)
            result[operation.container] = copy.deepcopy(container.get("resources", {}))
    except RawManifestPlanError as exc:
        raise FluxHelmReleasePlanError(f"post-render target container is unavailable: {exc}") from exc
    return result


def _temporary_values_path(
    snapshot_files: Mapping[str, Any],
    action: ActionPlanV3,
    source: WorkloadSource,
    base_revision: str,
) -> str:
    identity = json.dumps(
        [action.action_id, source.id, base_revision, _target_identity(action.target)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    candidate = f".greenkube-render/flux-values-{digest}.yaml"
    suffix = 0
    while candidate in snapshot_files:
        suffix += 1
        candidate = f".greenkube-render/flux-values-{digest}-{suffix}.yaml"
        if suffix > 64:
            raise FluxHelmReleasePlanError("could not reserve a unique inline-values render path")
    return candidate


def _read_source_file(
    root: Path,
    snapshot_files: Mapping[str, Any],
    path: str,
    error_type: type[HelmPlanError],
) -> str:
    try:
        return _read_snapshot_file(root, snapshot_files, path)
    except KustomizePlanError as exc:
        raise error_type(f"{path}: snapshot file could not be read safely") from exc


@contextmanager
def _stage(
    root: Path,
    snapshot_files: Mapping[str, Any],
    contents: Mapping[str, str],
    error_type: type[HelmPlanError],
) -> Iterator[None]:
    try:
        with _stage_files(root, snapshot_files, contents):
            yield
    except KustomizePlanError as exc:
        raise error_type("snapshot source could not be staged for Helm render validation") from exc


def _canonical_path(
    path: object,
    *,
    allow_root: bool,
    error: type[HelmPlanError],
) -> str:
    try:
        if not isinstance(path, str) or path != path.strip():
            raise KustomizePlanError("repository path must be a trimmed string")
        return _canonical_repo_path(path, allow_root=allow_root)
    except KustomizePlanError as exc:
        raise error("repository paths must be canonical snapshot-relative paths") from exc


def _chart_exists(snapshot_files: Mapping[str, Any], chart_path: str) -> bool:
    return _join_path(chart_path, "Chart.yaml") in snapshot_files


def _join_path(directory: str, relative: str) -> str:
    return relative if directory == "." else f"{directory}/{relative}"


def _is_in_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(f"{scope}/")


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, str):
        return str(value)
    return value


def _revision_commit(value: str) -> str | None:
    if _FULL_SHA.fullmatch(value):
        return value.lower()
    match = re.search(r"(?:sha1|sha256):([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$", value)
    return match.group(1).lower() if match is not None else None
