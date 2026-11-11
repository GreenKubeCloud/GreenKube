"""Plan safe, overlay-local Kustomize strategic patches."""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from greenkube.automation.actions import ACTION_KINDS, ActionKindSpec, Support
from greenkube.automation.actions.primitives import (
    ListSelector,
    PathSegment,
    PrimitiveOp,
    SetField,
    field_operations,
    format_path,
    read_field,
)
from greenkube.automation.actions.workloads import POD_SPEC_PATHS
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ControllerKind,
    FileEdit,
    FilePlan,
    RendererKind,
    ResourceTarget,
    TypedOperation,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.planning.digest import plan_digest
from greenkube.gitops.planning.raw_manifest import (
    RawManifestPlanError,
    _action_kind,
    _plan_identifier,
    _rendered_json,
    _rendered_target_matches,
    _resources_by_identity,
    _target_identity,
    _unified_diff,
    _validate_rendered_change,
)
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from greenkube.gitops.renderers.raw import RawRenderResult
from greenkube.gitops.sha import GIT_OBJECT_SHA, SHA256_DIGEST
from greenkube.gitops.snapshots.models import RepositorySnapshot

KUSTOMIZE_VERSION = "v5.8.3"

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_SUPPORTED_KUSTOMIZATION_KEYS = frozenset(
    {
        "apiVersion",
        "kind",
        "resources",
        "bases",
        "patches",
        "patchesStrategicMerge",
        "patchesJson6902",
        "namespace",
        "namePrefix",
        "nameSuffix",
        "commonLabels",
        "commonAnnotations",
        "labels",
        "images",
        "replicas",
        "buildMetadata",
        "sortOptions",
        "components",
        "helmCharts",
        "helmGlobals",
    }
)
_REMOTE_HOST = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::[0-9]+)?/")
_PATCH_ENTRY_KEYS = frozenset({"path", "patch", "target", "options"})
_PATCH_TARGET_KEYS = frozenset({"group", "version", "kind", "name", "namespace", "labelSelector", "annotationSelector"})
_PATCH_METADATA_MARKERS = frozenset({"greenkube.cloud/managed", "greenkube.cloud/action-id"})
#: The strategic-merge key GreenKube patches use to address list entries.
_PATCH_LIST_KEY = "name"


class KustomizePlanError(ValueError):
    """The requested action cannot be applied safely to a Kustomize overlay."""


class KustomizePlanner:
    """Create deterministic, render-validated FilePlans for one resolved overlay.

    This is the Kustomize ``PatchStrategy``: the action kind compiles its typed operations
    to primitive ``SetField`` edits, which become one overlay-local, GreenKube-managed
    strategic-merge patch (merged with the fields an earlier managed patch already sets).
    """

    renderer_kind = RendererKind.KUSTOMIZE

    def supports(self, op: Any, binding: WorkloadSource) -> Support:
        del binding
        return _primitive_support(op)

    def __init__(
        self,
        *,
        renderer: KustomizeRenderer | None = None,
        runner: Any | None = None,
        expected_version: str = KUSTOMIZE_VERSION,
        binary: str = "kustomize",
    ) -> None:
        if renderer is not None and runner is not None:
            raise ValueError("supply either a Kustomize renderer or a runner, not both")
        self.renderer = renderer or KustomizeRenderer(
            expected_version=expected_version,
            binary=binary,
            runner=runner,
        )

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: RepositorySnapshot,
        base_revision: str,
    ) -> FilePlan:
        spec, operations = _validate_request(action, source, snapshot, base_revision)
        root = _snapshot_root(snapshot)
        snapshot_files = _snapshot_file_map(snapshot)
        _validate_workspace_inventory(root, snapshot_files)
        _validate_renderer_config_paths(source, snapshot_files)
        overlay_path = _overlay_path(source)
        kustomizations = _kustomization_index(snapshot_files)
        if overlay_path not in kustomizations:
            raise KustomizePlanError("resolved overlay does not contain a Kustomization file in the snapshot")

        configs = _validate_render_graph(root, snapshot_files, kustomizations, overlay_path)
        if _find_overlay_consumers(root, snapshot_files, kustomizations, overlay_path, configs):
            raise KustomizePlanError("refusing to edit a shared base referenced by another Kustomize consumer")

        kustomization_path = kustomizations[overlay_path]
        _validate_kustomization_hint(source, overlay_path)
        config = configs[overlay_path]
        overrides = _flux_build_overrides(source, config)
        original_kustomization = _read_snapshot_file(root, snapshot_files, kustomization_path)
        before = _render_as_deployed(
            self.renderer,
            snapshot,
            root,
            snapshot_files,
            overlay_path,
            kustomization_path,
            original_kustomization,
            overrides,
        )
        allow_namespace_less = controller_supplies_namespace(source)
        rendered_target = _require_target(before, action.target, allow_namespace_less=allow_namespace_less)
        _reject_post_build_variables(before, rendered_target, overrides, _patch_fields(spec, action.target, operations))
        selector = _target_selector(
            _patch_selector_target(
                self.renderer,
                snapshot,
                root,
                snapshot_files,
                overlay_path,
                kustomization_path,
                original_kustomization,
                config,
                overrides,
                rendered_target,
            )
        )

        expected_path = _managed_patch_path(overlay_path, action.target)
        patch_path, preserved_labels, preserved_annotations, preserved_fields = _find_managed_patch(
            root,
            snapshot_files,
            overlay_path,
            kustomization_path,
            config,
            expected_path,
            action.target,
            selector,
        )
        patch_content = _patch_content(
            action.target,
            _patch_fields(spec, action.target, operations),
            preserved_labels=preserved_labels,
            preserved_annotations=preserved_annotations,
            preserved_fields=preserved_fields,
            rendered_target=_rendered_manifest(before, rendered_target),
        )
        updated_kustomization = _add_patch_entry(
            config,
            original_kustomization,
            overlay_path,
            patch_path,
            selector,
            root,
            snapshot_files,
        )

        original_patch = snapshot_files.get(patch_path)
        if original_patch is not None:
            original_patch_content = _read_snapshot_file(root, snapshot_files, patch_path)
        else:
            original_patch_content = None

        after = _render_as_deployed(
            self.renderer,
            snapshot,
            root,
            snapshot_files,
            overlay_path,
            kustomization_path,
            updated_kustomization,
            overrides,
            extra_files={patch_path: patch_content},
        )

        _validate_render_diff(before, after, action.target, action.target.kind, operations)
        if _require_target(after, action.target, allow_namespace_less=allow_namespace_less) != rendered_target:
            raise KustomizePlanError("Kustomize render validation changed the rendered target identity")
        before_resources = _resources_by_identity(before)
        after_resources = _resources_by_identity(after)
        target_identity = _target_identity(rendered_target)
        target_changed = before_resources[target_identity] != after_resources[target_identity]
        if not target_changed:
            raise KustomizePlanError(f"the requested {spec.description} are already present in the rendered target")

        edits: list[FileEdit] = []
        originals: dict[str, str] = {}
        if original_kustomization != updated_kustomization:
            edits.append(
                FileEdit(
                    path=kustomization_path,
                    operation="update",
                    original_sha=_git_blob_sha(original_kustomization.encode("utf-8"), base_revision),
                    content=updated_kustomization,
                )
            )
            originals[kustomization_path] = original_kustomization
        if original_patch_content != patch_content:
            if original_patch_content is None:
                operation = "create"
                original_sha = None
            else:
                operation = "update"
                original_sha = _git_blob_sha(original_patch_content.encode("utf-8"), base_revision)
                originals[patch_path] = original_patch_content
            edits.append(
                FileEdit(
                    path=patch_path,
                    operation=operation,
                    original_sha=original_sha,
                    content=patch_content,
                )
            )
        if not edits:
            raise KustomizePlanError("the requested Kustomize patch produces no source changes")

        edits.sort(key=lambda edit: edit.path)
        diff = _file_diff(edits, originals)
        rendered_diff = _unified_diff(
            _rendered_json(before),
            _rendered_json(after),
            "rendered-before",
            "rendered-after",
        )
        warnings = sorted(set(before.warnings + after.warnings))
        plan_id = _plan_identifier(action, source, base_revision)
        validation = ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.KUSTOMIZE,
            output_digest=after.output_digest,
            warnings=warnings,
        )
        source_revision = source.source_revision or source.revision
        digest = plan_digest(
            plan_id=plan_id,
            action=action,
            source=source,
            files=sorted(edits, key=lambda item: item.path),
            diff=diff,
            rendered_diff=rendered_diff,
            base_revision=base_revision,
            source_revision=source_revision,
            validation=validation,
            warnings=warnings,
            renderer=RendererKind.KUSTOMIZE,
            error_type=KustomizePlanError,
        )
        return FilePlan(
            plan_id=plan_id,
            files=edits,
            diff=diff,
            rendered_diff=rendered_diff,
            base_revision=base_revision,
            source_revision=source_revision,
            renderer=RendererKind.KUSTOMIZE,
            validation=validation,
            warnings=warnings,
            digest=digest,
        )


def plan_kustomize(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: RepositorySnapshot,
    base_revision: str,
) -> FilePlan:
    """Plan one Kustomize overlay action without retaining workspace edits."""

    return KustomizePlanner().plan(action, source, snapshot, base_revision)


def controller_supplies_namespace(source: WorkloadSource) -> bool:
    """Whether the GitOps controller namespaces the overlay's namespace-less resources itself.

    Only such a controller (an Argo CD destination) lets a namespace-less rendered workload
    match the live one; Flux namespaces them through ``targetNamespace`` in the build.
    """
    return source.controller_kind is not ControllerKind.FLUX_KUSTOMIZATION


def render_as_deployed(
    renderer: KustomizeRenderer,
    snapshot: RepositorySnapshot,
    source: WorkloadSource,
) -> RawRenderResult:
    """Render a Kustomize source the way its GitOps controller builds it.

    Flux ``targetNamespace``/``namePrefix``/``nameSuffix`` are applied to the root
    Kustomization for the render only, so rendered identities match the live cluster.
    """
    root = _snapshot_root(snapshot)
    snapshot_files = _snapshot_file_map(snapshot)
    overlay_path = _overlay_path(source)
    kustomization_path = _kustomization_index(snapshot_files).get(overlay_path)
    if kustomization_path is None:
        raise KustomizePlanError("resolved overlay does not contain a Kustomization file in the snapshot")
    config = _load_kustomization(root, snapshot_files, kustomization_path)
    return _render_as_deployed(
        renderer,
        snapshot,
        root,
        snapshot_files,
        overlay_path,
        kustomization_path,
        _read_snapshot_file(root, snapshot_files, kustomization_path),
        _flux_build_overrides(source, config),
    )


def _validate_request(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: RepositorySnapshot,
    base_revision: str,
) -> tuple[ActionKindSpec, list[TypedOperation]]:
    if not isinstance(snapshot, RepositorySnapshot):
        raise KustomizePlanError("snapshot must be an archive RepositorySnapshot")
    if not isinstance(base_revision, str) or not GIT_OBJECT_SHA.fullmatch(base_revision):
        raise KustomizePlanError("resolved base revision must be a full 40- or 64-character commit SHA")
    if base_revision.lower() != snapshot.metadata.commit_sha.lower():
        raise KustomizePlanError("base revision does not match the archive snapshot commit")
    if source.provider_id != snapshot.metadata.provider_id or source.repo_url != snapshot.metadata.repo_url:
        raise KustomizePlanError("workload source repository does not match the archive snapshot identity")
    if source.renderer is not RendererKind.KUSTOMIZE:
        raise KustomizePlanError("workload source must use the Kustomize renderer")
    if source.status is not BindingStatus.RESOLVED:
        raise KustomizePlanError("workload source binding must be resolved")
    if (
        action.target.kind != source.workload_kind
        or action.target.namespace != source.namespace
        or action.target.name != source.workload_name
    ):
        raise KustomizePlanError("action target must match the resolved workload binding")
    spec, operations = _action_kind(action, KustomizePlanError)
    support = spec.renderer_support(RendererKind.KUSTOMIZE, operations)
    if support.ok:
        support = next(
            (item for item in map(_primitive_support, spec.compile(action.target, operations)) if not item.ok),
            support,
        )
    if not support.ok:
        raise KustomizePlanError(support.reason or "the action cannot be expressed as a Kustomize patch")
    _validate_renderer_config(source)
    return spec, operations


def _primitive_support(op: PrimitiveOp) -> Support:
    """Kustomize v1 writes ``SetField`` edits under ``spec`` addressed by ``name`` list keys."""
    if not isinstance(op, SetField):
        return Support.unsupported(f"Kustomize planning does not support {op.op!r} primitives yet")
    segments = op.segments
    if segments[0] != "spec":
        return Support.unsupported("Kustomize planning patches fields under spec only")
    if any(isinstance(segment, ListSelector) and segment.key != _PATCH_LIST_KEY for segment in segments):
        return Support.unsupported("Kustomize strategic-merge patches address list entries by name only")
    return Support.supported()


def _patch_fields(
    spec: ActionKindSpec,
    target: ResourceTarget,
    operations: Sequence[TypedOperation],
) -> dict[tuple[PathSegment, ...], Any]:
    return {
        primitive.segments: primitive.value
        for primitive in field_operations(spec.compile(target, operations))
        if isinstance(primitive, SetField)
    }


def _validate_renderer_config(source: WorkloadSource) -> None:
    config = source.renderer_config
    if not isinstance(config, Mapping):
        raise KustomizePlanError("renderer configuration must be a mapping")
    supported = {
        "overlay_path",
        "kustomization_path",
        "base_paths",
        "source_ref",
        "target_namespace",
        "name_prefix",
        "name_suffix",
        "post_build",
        "patches",
        "components",
        "images",
        "helm_charts",
    }
    unknown = set(config) - supported
    if unknown:
        raise KustomizePlanError(f"unsupported Kustomize renderer configuration: {', '.join(sorted(unknown))}")
    for key in ("components", "helm_charts", "patches", "images"):
        value = config.get(key)
        if value not in (None, [], {}, ""):
            feature = "Helm charts" if key == "helm_charts" else key.replace("_", " ")
            raise KustomizePlanError(
                f"renderer override {feature!r} is not supported: the GitOps controller would change "
                "the build output after GreenKube's patch"
            )
    base_paths = config.get("base_paths", [])
    if base_paths is None:
        base_paths = []
    if not isinstance(base_paths, list):
        raise KustomizePlanError("renderer base_paths must be a list of snapshot-relative paths")
    for path in base_paths:
        _canonical_repo_path(path, allow_root=True)
    if "overlay_path" in config and config["overlay_path"] != source.path:
        raise KustomizePlanError("renderer overlay_path must match the resolved workload source path")


@dataclass(frozen=True, slots=True)
class _FluxBuildOverrides:
    """Fields a Flux Kustomization writes into the root Kustomization before building.

    Flux's kustomize-controller sets (or overrides) ``namespace`` from
    ``spec.targetNamespace`` and ``namePrefix``/``nameSuffix`` from the matching
    spec fields, then runs ``kustomize build`` and finally applies
    ``spec.postBuild`` variable substitution to the output.
    """

    target_namespace: str | None = None
    name_prefix: str | None = None
    name_suffix: str | None = None
    post_build: bool = False

    @property
    def rewrites_kustomization(self) -> bool:
        return any(value is not None for value in (self.target_namespace, self.name_prefix, self.name_suffix))


def _flux_build_overrides(source: WorkloadSource, config: Mapping[str, Any]) -> _FluxBuildOverrides:
    renderer_config = source.renderer_config
    target_namespace = _optional_override(renderer_config, "target_namespace", "Flux targetNamespace")
    if target_namespace is not None and target_namespace != source.namespace:
        raise KustomizePlanError(
            "unsupported Flux targetNamespace: it must match the resolved workload namespace "
            "(Flux sets every namespaced resource of the overlay to targetNamespace)"
        )
    name_prefix = _optional_override(renderer_config, "name_prefix", "Flux namePrefix")
    name_suffix = _optional_override(renderer_config, "name_suffix", "Flux nameSuffix")
    for value, field_name, label in (
        (name_prefix, "namePrefix", "Flux namePrefix"),
        (name_suffix, "nameSuffix", "Flux nameSuffix"),
    ):
        if value is not None and config.get(field_name):
            raise KustomizePlanError(
                f"unsupported {label}: the overlay also sets {field_name}, so the deployed workload name is ambiguous"
            )
    post_build = renderer_config.get("post_build", False)
    if post_build is None:
        post_build = False
    if not isinstance(post_build, bool):
        raise KustomizePlanError("renderer post_build must be a boolean")
    return _FluxBuildOverrides(
        target_namespace=target_namespace,
        name_prefix=name_prefix,
        name_suffix=name_suffix,
        post_build=post_build,
    )


def _optional_override(renderer_config: Mapping[str, Any], key: str, label: str) -> str | None:
    value = renderer_config.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise KustomizePlanError(f"unsupported {label}: it must be a non-empty string")
    return value


def _overlay_path(source: WorkloadSource) -> str:
    try:
        path = _canonical_repo_path(source.path, allow_root=True)
    except KustomizePlanError as exc:
        raise KustomizePlanError("resolved overlay path must be a canonical snapshot-relative path") from exc
    return path


def _canonical_repo_path(path: object, *, allow_root: bool) -> str:
    if not isinstance(path, str) or not path or path != path.strip() or "\x00" in path or "\\" in path:
        raise KustomizePlanError("snapshot paths must be canonical relative POSIX paths")
    if path == "." and allow_root:
        return "."
    if (
        path.startswith("/")
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).drive
        or PurePosixPath(path).as_posix() != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise KustomizePlanError("snapshot paths must remain inside the repository")
    return path


def _snapshot_root(snapshot: RepositorySnapshot) -> Path:
    try:
        workspace = Path(snapshot.workspace)
        if workspace.is_symlink():
            raise KustomizePlanError("snapshot workspace must not be a symbolic link")
        root = workspace.resolve(strict=True)
    except KustomizePlanError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise KustomizePlanError("snapshot workspace must be an existing directory") from exc
    if not root.is_dir():
        raise KustomizePlanError("snapshot workspace must be an existing directory")
    return root


def _snapshot_file_map(snapshot: RepositorySnapshot) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for item in snapshot.files:
        path = _canonical_repo_path(item.path, allow_root=False)
        if path in files:
            raise KustomizePlanError(f"{path}: archive snapshot contains duplicate file metadata")
        if type(item.size_bytes) is not int or item.size_bytes < 0:
            raise KustomizePlanError(f"{path}: archive snapshot file size is invalid")
        if not isinstance(item.digest, str) or not SHA256_DIGEST.fullmatch(item.digest):
            raise KustomizePlanError(f"{path}: archive snapshot file digest is invalid")
        files[path] = item
    return files


def _validate_workspace_inventory(root: Path, files: Mapping[str, Any]) -> None:
    observed: set[str] = set()
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise KustomizePlanError("snapshot workspace must not contain symbolic links")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                observed.add(_canonical_repo_path(relative, allow_root=False))
            elif not path.is_dir():
                raise KustomizePlanError("snapshot workspace contains an unsupported filesystem entry")
    except KustomizePlanError:
        raise
    except OSError as exc:
        raise KustomizePlanError("snapshot workspace could not be inspected") from exc
    if observed != set(files):
        raise KustomizePlanError("snapshot workspace files do not match the archive snapshot file list")


def _read_snapshot_file(root: Path, files: Mapping[str, Any], relative_path: str) -> str:
    path = _canonical_repo_path(relative_path, allow_root=False)
    item = files.get(path)
    if item is None:
        raise KustomizePlanError(f"{path}: referenced file is not present in the archive snapshot")
    candidate = root.joinpath(*PurePosixPath(path).parts)
    current = root
    for part in PurePosixPath(path).parts:
        current = current / part
        if current.is_symlink():
            raise KustomizePlanError(f"{path}: snapshot paths must not traverse symbolic links")
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise KustomizePlanError(f"{path}: file path must remain inside the archive snapshot")
        content = resolved.read_bytes()
    except KustomizePlanError:
        raise
    except (OSError, RuntimeError) as exc:
        raise KustomizePlanError(f"{path}: snapshot file could not be read") from exc
    if len(content) != item.size_bytes or f"sha256:{hashlib.sha256(content).hexdigest()}" != item.digest:
        raise KustomizePlanError(f"{path}: snapshot file content no longer matches its archive digest")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KustomizePlanError(f"{path}: Kustomize configuration and patches must be valid UTF-8") from exc


def _kustomization_index(files: Mapping[str, Any]) -> dict[str, str]:
    paths: dict[str, list[str]] = {}
    for path in files:
        pure_path = PurePosixPath(path)
        if pure_path.name in _KUSTOMIZATION_NAMES:
            directory = pure_path.parent.as_posix()
            paths.setdefault(directory, []).append(path)
    result: dict[str, str] = {}
    for directory, candidates in paths.items():
        if len(candidates) != 1:
            raise KustomizePlanError(f"{directory}: multiple Kustomization file names are ambiguous")
        result[directory] = candidates[0]
    return result


def _validate_render_graph(
    root: Path,
    files: Mapping[str, Any],
    kustomizations: Mapping[str, str],
    overlay_path: str,
) -> dict[str, MutableMapping[str, Any]]:
    configs: dict[str, MutableMapping[str, Any]] = {}
    active: set[str] = set()

    def visit(directory: str) -> None:
        if directory in active:
            raise KustomizePlanError("recursive Kustomize base references are unsupported")
        if directory in configs:
            return
        kustomization_path = kustomizations.get(directory)
        if kustomization_path is None:
            raise KustomizePlanError(f"{directory}: Kustomize directory has no Kustomization file")
        config = _load_kustomization(root, files, kustomization_path)
        _validate_kustomization_schema(config, kustomization_path)
        components = _string_list(config, "components", kustomization_path)
        if components:
            raise KustomizePlanError(f"{kustomization_path}: Kustomize components are unsupported")
        helm_charts = config.get("helmCharts", [])
        if helm_charts not in (None, []):
            raise KustomizePlanError(f"{kustomization_path}: Kustomize Helm charts are unsupported")
        configs[directory] = config
        active.add(directory)
        try:
            for field_name in ("resources", "bases"):
                for reference in _string_list(config, field_name, kustomization_path):
                    resolved = _resolve_reference(directory, reference)
                    if resolved in kustomizations:
                        visit(resolved)
                    elif resolved in files:
                        _read_snapshot_file(root, files, resolved)
                    else:
                        raise KustomizePlanError(
                            f"{kustomization_path}: resource path {reference!r} is not in the archive snapshot"
                        )
            _validate_patch_references(root, files, directory, config, kustomization_path)
        finally:
            active.remove(directory)

    visit(overlay_path)
    return configs


def _load_kustomization(root: Path, files: Mapping[str, Any], path: str) -> MutableMapping[str, Any]:
    content = _read_snapshot_file(root, files, path)
    yaml = _roundtrip_yaml(content)
    try:
        documents = list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizePlanError(f"{path}: Kustomization YAML is invalid") from exc
    if len(documents) != 1 or not isinstance(documents[0], MutableMapping):
        raise KustomizePlanError(f"{path}: Kustomization must contain exactly one mapping document")
    config = documents[0]
    if any(not isinstance(key, str) for key in config):
        raise KustomizePlanError(f"{path}: Kustomization keys must be strings")
    return config


def _roundtrip_yaml(content: str) -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4_096
    if "\r\n" in content:
        yaml.line_break = "\r\n"
    return yaml


def _validate_kustomization_schema(config: Mapping[str, Any], path: str) -> None:
    unknown = set(config) - _SUPPORTED_KUSTOMIZATION_KEYS
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise KustomizePlanError(f"{path}: unsupported Kustomization schema fields: {fields}")

    api_version = config.get("apiVersion")
    kind = config.get("kind")
    if api_version is not None or kind is not None:
        if not isinstance(api_version, str) or api_version not in {
            "kustomize.config.k8s.io/v1beta1",
            "kustomize.config.k8s.io/v1",
        }:
            raise KustomizePlanError(f"{path}: unsupported Kustomization schema apiVersion")
        if kind == "Component":
            raise KustomizePlanError(f"{path}: Kustomize components are unsupported")
        if kind != "Kustomization":
            raise KustomizePlanError(f"{path}: unsupported Kustomization schema kind")
    for field_name in ("namespace", "namePrefix", "nameSuffix"):
        if field_name in config and not isinstance(config[field_name], str):
            raise KustomizePlanError(f"{path}: {field_name} must be a string")
    for field_name in ("resources", "bases", "components", "patchesStrategicMerge", "buildMetadata"):
        if field_name in config:
            _string_list(config, field_name, path)
    if config.get("patchesStrategicMerge"):
        raise KustomizePlanError(f"{path}: legacy patchesStrategicMerge is unsupported; use patches")
    if config.get("patchesJson6902"):
        raise KustomizePlanError(f"{path}: legacy patchesJson6902 is unsupported; use patches")

    for field_name in ("patches", "patchesJson6902"):
        if field_name in config and not isinstance(config[field_name], list):
            raise KustomizePlanError(f"{path}: {field_name} must be a list")
    if "helmCharts" in config and config["helmCharts"] not in (None, []):
        raise KustomizePlanError(f"{path}: Kustomize Helm charts are unsupported")


def _string_list(config: Mapping[str, Any], field_name: str, path: str) -> tuple[str, ...]:
    values = config.get(field_name, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise KustomizePlanError(f"{path}: {field_name} must be a list of local paths")
    return tuple(values)


def _validate_patch_references(
    root: Path,
    files: Mapping[str, Any],
    directory: str,
    config: Mapping[str, Any],
    kustomization_path: str,
) -> None:
    patches = config.get("patches", [])
    if not isinstance(patches, list):
        raise KustomizePlanError(f"{kustomization_path}: patches must be a list")
    for entry in patches:
        if not isinstance(entry, Mapping) or set(entry) - _PATCH_ENTRY_KEYS:
            raise KustomizePlanError(f"{kustomization_path}: unsupported patches entry schema")
        has_path = "path" in entry
        has_patch = "patch" in entry
        if has_path == has_patch:
            raise KustomizePlanError(f"{kustomization_path}: patches entries require exactly one path or patch")
        if has_path:
            reference = entry["path"]
            if not isinstance(reference, str):
                raise KustomizePlanError(f"{kustomization_path}: patch path must be a string")
            resolved = _resolve_reference(directory, reference)
            _read_snapshot_file(root, files, resolved)
        elif not isinstance(entry["patch"], str):
            raise KustomizePlanError(f"{kustomization_path}: inline patch must be a string")
        if "target" in entry:
            target = entry["target"]
            if not isinstance(target, Mapping) or set(target) - _PATCH_TARGET_KEYS:
                raise KustomizePlanError(f"{kustomization_path}: patch target schema is unsupported")
        if "options" in entry and not isinstance(entry["options"], Mapping):
            raise KustomizePlanError(f"{kustomization_path}: patch options must be a mapping")

    legacy_patches = config.get("patchesStrategicMerge", [])
    if not isinstance(legacy_patches, list) or any(not isinstance(path, str) for path in legacy_patches):
        raise KustomizePlanError(f"{kustomization_path}: patchesStrategicMerge must be a list of paths")
    for reference in legacy_patches:
        _read_snapshot_file(root, files, _resolve_reference(directory, reference))

    json_patches = config.get("patchesJson6902", [])
    if not isinstance(json_patches, list):
        raise KustomizePlanError(f"{kustomization_path}: patchesJson6902 must be a list")
    for entry in json_patches:
        if not isinstance(entry, Mapping) or set(entry) - {"target", "path", "patch"}:
            raise KustomizePlanError(f"{kustomization_path}: patchesJson6902 entry schema is unsupported")
        has_path = "path" in entry
        has_patch = "patch" in entry
        if has_path == has_patch:
            raise KustomizePlanError(f"{kustomization_path}: JSON patches require exactly one path or patch")
        if "target" in entry and (
            not isinstance(entry["target"], Mapping) or set(entry["target"]) - _PATCH_TARGET_KEYS
        ):
            raise KustomizePlanError(f"{kustomization_path}: JSON patch target schema is unsupported")
        reference = entry.get("path")
        if reference is not None:
            if not isinstance(reference, str):
                raise KustomizePlanError(f"{kustomization_path}: JSON patch path must be a string")
            _read_snapshot_file(root, files, _resolve_reference(directory, reference))
        elif not isinstance(entry.get("patch"), list):
            raise KustomizePlanError(f"{kustomization_path}: JSON patch requires a path or patch list")


def _resolve_reference(directory: str, reference: str) -> str:
    if (
        not isinstance(reference, str)
        or not reference
        or reference != reference.strip()
        or "\\" in reference
        or "\x00" in reference
    ):
        raise KustomizePlanError("Kustomize references must be relative POSIX paths inside the snapshot")
    if _is_remote_reference(reference):
        raise KustomizePlanError("remote Kustomize bases and resources are unsupported")
    if reference.startswith("/") or PurePosixPath(reference).is_absolute() or PureWindowsPath(reference).drive:
        raise KustomizePlanError("Kustomize references must be relative POSIX paths inside the snapshot")
    if any(part == "" for part in reference.split("/")):
        raise KustomizePlanError("Kustomize references must not contain empty path components")
    base = "" if directory == "." else directory
    normalized = posixpath.normpath(posixpath.join(base, reference))
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise KustomizePlanError("Kustomize reference escapes the archive snapshot")
    return "." if normalized == "." else _canonical_repo_path(normalized, allow_root=False)


def _is_remote_reference(reference: str) -> bool:
    try:
        parsed = urlsplit(reference.strip())
    except ValueError:
        return True
    return (
        bool(parsed.scheme)
        or reference.startswith("//")
        or reference.startswith("git@")
        or _REMOTE_HOST.match(reference) is not None
    )


def _find_overlay_consumers(
    root: Path,
    files: Mapping[str, Any],
    kustomizations: Mapping[str, str],
    overlay_path: str,
    parsed_configs: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    reverse_edges: dict[str, set[str]] = {}
    for consumer, path in kustomizations.items():
        config = parsed_configs.get(consumer)
        if config is None:
            config = _load_kustomization(root, files, path)
        for field_name in ("resources", "bases", "components"):
            for reference in _string_list(config, field_name, path):
                if _is_remote_reference(reference):
                    continue
                dependency = _resolve_reference(consumer, reference)
                if dependency in kustomizations:
                    reverse_edges.setdefault(dependency, set()).add(consumer)

    consumers: set[str] = set()
    pending = [overlay_path]
    visited = {overlay_path}
    while pending:
        dependency = pending.pop()
        for consumer in reverse_edges.get(dependency, set()):
            if consumer == overlay_path or consumer in visited:
                continue
            consumers.add(consumer)
            visited.add(consumer)
            pending.append(consumer)
    return consumers


def _validate_kustomization_hint(source: WorkloadSource, overlay_path: str) -> None:
    hint = source.renderer_config.get("kustomization_path")
    if hint is None:
        return
    canonical = _canonical_repo_path(hint, allow_root=False)
    if (
        PurePosixPath(canonical).parent.as_posix() != overlay_path
        or PurePosixPath(canonical).name not in _KUSTOMIZATION_NAMES
    ):
        raise KustomizePlanError("renderer kustomization_path does not identify the resolved overlay file")


def _validate_renderer_config_paths(source: WorkloadSource, files: Mapping[str, Any]) -> None:
    config = source.renderer_config
    for raw_path in config.get("base_paths") or []:
        path = _canonical_repo_path(raw_path, allow_root=True)
        if path != "." and path not in files and not any(existing.startswith(f"{path}/") for existing in files):
            raise KustomizePlanError(f"renderer base path {path!r} is not present in the archive snapshot")


def _managed_patch_path(overlay_path: str, target: ResourceTarget) -> str:
    identity = json.dumps(
        [target.api_version, target.kind, target.namespace, target.name],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{target.kind}-{target.name}".lower()).strip("-")[:48] or "workload"
    relative = f"greenkube-patches/greenkube-{slug}-{digest}.yaml"
    return relative if overlay_path == "." else f"{overlay_path}/{relative}"


def _managed_filename_prefix(target: ResourceTarget) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{target.kind}-{target.name}".lower()).strip("-")[:48] or "workload"
    return f"greenkube-{slug}-"


def _find_managed_patch(
    root: Path,
    files: Mapping[str, Any],
    overlay_path: str,
    kustomization_path: str,
    config: Mapping[str, Any],
    expected_path: str,
    target: ResourceTarget,
    expected_selector: Mapping[str, str],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[tuple[PathSegment, ...], Any]]:
    prefix = _managed_filename_prefix(target)
    patch_entries = config.get("patches", [])
    referenced: dict[str, Mapping[str, Any]] = {}
    for entry in patch_entries:
        if isinstance(entry, Mapping) and isinstance(entry.get("path"), str):
            resolved = _resolve_reference(overlay_path, entry["path"])
            referenced[resolved] = entry

    candidate_paths = {
        path
        for path in files
        if _is_within_overlay(path, overlay_path)
        and PurePosixPath(path).name.startswith(prefix)
        and re.fullmatch(r"[0-9a-f]{8,64}\.yaml", PurePosixPath(path).name[len(prefix) :]) is not None
    }
    candidate_paths.add(expected_path) if expected_path in files else None

    for path, entry in referenced.items():
        if path not in files:
            continue
        marked = _patch_has_managed_marker(_read_snapshot_file(root, files, path), target)
        if path in candidate_paths or marked:
            if not _is_within_overlay(path, overlay_path):
                raise KustomizePlanError("managed GreenKube patches must live inside the resolved overlay")
            candidate_paths.add(path)

    matches: list[tuple[str, dict[str, Any], dict[str, Any], dict[tuple[PathSegment, ...], Any]]] = []
    for path in sorted(candidate_paths):
        if path not in files:
            if path == expected_path:
                continue
            raise KustomizePlanError(f"{path}: managed patch path is not present in the archive snapshot")
        content = _read_snapshot_file(root, files, path)
        parsed = _parse_patch(content, path)
        if not _patch_targets_resource(parsed, target):
            if path == expected_path:
                raise KustomizePlanError(f"{path}: deterministic GreenKube patch path collides with another resource")
            continue
        labels, annotations, fields = _validate_managed_patch(parsed, path, target)
        if path in referenced:
            entry = referenced[path]
            if entry.get("target") != expected_selector or set(entry) != {"path", "target"}:
                raise KustomizePlanError(f"{kustomization_path}: managed patch requires an exact target selector")
        matches.append((path, labels, annotations, fields))

    if len(matches) > 1:
        raise KustomizePlanError(f"{kustomization_path}: multiple GreenKube patches target the same workload")
    if matches:
        return matches[0]
    if expected_path in files:
        raise KustomizePlanError(f"{expected_path}: existing file is not a valid managed GreenKube patch")
    return expected_path, {}, {}, {}


def _is_within_overlay(path: str, overlay_path: str) -> bool:
    if overlay_path == ".":
        relative = path
    elif path.startswith(f"{overlay_path}/"):
        relative = path.removeprefix(f"{overlay_path}/")
    else:
        return False
    parts = PurePosixPath(relative).parts
    return len(parts) == 1 or (len(parts) == 2 and parts[0] == "greenkube-patches")


def _patch_has_managed_marker(content: str, target: ResourceTarget) -> bool:
    try:
        document = _parse_patch(content, "managed patch")
    except KustomizePlanError:
        return False
    if not _patch_targets_resource(document, target):
        return False
    metadata = document.get("metadata", {})
    for field_name in ("labels", "annotations"):
        values = metadata.get(field_name, {}) if isinstance(metadata, Mapping) else {}
        if isinstance(values, Mapping) and values.get("greenkube.cloud/managed") == "true":
            return True
    return False


def _parse_patch(content: str, path: str) -> MutableMapping[str, Any]:
    yaml = _roundtrip_yaml(content)
    try:
        documents = list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizePlanError(f"{path}: managed patch YAML is invalid") from exc
    if len(documents) != 1 or not isinstance(documents[0], MutableMapping):
        raise KustomizePlanError(f"{path}: managed patch must contain exactly one mapping document")
    return documents[0]


def _patch_targets_resource(document: Mapping[str, Any], target: ResourceTarget) -> bool:
    metadata = document.get("metadata")
    return (
        document.get("apiVersion") == target.api_version
        and document.get("kind") == target.kind
        and isinstance(metadata, Mapping)
        and metadata.get("name") == target.name
        and metadata.get("namespace") == target.namespace
    )


def _validate_managed_patch(
    document: Mapping[str, Any],
    path: str,
    target: ResourceTarget,
) -> tuple[dict[str, Any], dict[str, Any], dict[tuple[PathSegment, ...], Any]]:
    """Validate a GreenKube-managed patch; return its markers and the fields it sets.

    Every field under ``spec`` must belong to a registered action kind (for container
    resources: CPU/memory requests of named containers), so a hand-edited patch fails
    closed instead of being rewritten.
    """
    if set(document) - {"apiVersion", "kind", "metadata", "spec"}:
        raise KustomizePlanError(f"{path}: managed patch contains unsupported fields")
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping) or set(metadata) - {"name", "namespace", "labels", "annotations"}:
        raise KustomizePlanError(f"{path}: managed patch metadata is unsupported")
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    for field_name, values in (("labels", labels), ("annotations", annotations)):
        if not isinstance(values, Mapping) or set(values) - _PATCH_METADATA_MARKERS:
            raise KustomizePlanError(f"{path}: managed patch {field_name} are unsupported")
    if "greenkube.cloud/managed" in labels and labels["greenkube.cloud/managed"] != "true":
        raise KustomizePlanError(f"{path}: managed patch label has an invalid value")
    if "greenkube.cloud/managed" in annotations and annotations["greenkube.cloud/managed"] != "true":
        raise KustomizePlanError(f"{path}: managed patch annotation has an invalid value")
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        raise KustomizePlanError(f"{path}: managed patch spec must be a mapping")
    fields: dict[tuple[PathSegment, ...], Any] = {}
    _collect_patch_fields(spec, ("spec",), path, fields)
    for segments, value in fields.items():
        if not ACTION_KINDS.owns_patch_field(target.kind, segments, value):
            raise KustomizePlanError(
                f"{path}: managed patch contains a field GreenKube does not manage: {format_path(segments)}"
            )
    return dict(labels), dict(annotations), fields


def _collect_patch_fields(
    node: Any,
    segments: tuple[PathSegment, ...],
    path: str,
    fields: dict[tuple[PathSegment, ...], Any],
) -> None:
    """Flatten a strategic-merge patch into ``segments -> value`` leaves (lists keyed by name)."""
    if isinstance(node, Mapping):
        if not node:
            raise KustomizePlanError(f"{path}: managed patch contains an empty mapping")
        for key, value in node.items():
            if not isinstance(key, str) or not key:
                raise KustomizePlanError(f"{path}: managed patch field names are invalid")
            _collect_patch_fields(value, (*segments, key), path, fields)
        return
    if isinstance(node, list):
        if not node:
            raise KustomizePlanError(f"{path}: managed patch lists must not be empty")
        names: set[str] = set()
        for item in node:
            name = item.get(_PATCH_LIST_KEY) if isinstance(item, Mapping) else None
            if not isinstance(name, str) or not name or name in names or len(item) < 2:
                raise KustomizePlanError(f"{path}: managed patch list entries need unique names and fields")
            names.add(name)
            selector = ListSelector(_PATCH_LIST_KEY, name)
            for key, value in item.items():
                if key == _PATCH_LIST_KEY:
                    continue
                if not isinstance(key, str) or not key:
                    raise KustomizePlanError(f"{path}: managed patch field names are invalid")
                _collect_patch_fields(value, (*segments, selector, key), path, fields)
        return
    fields[segments] = node


def _target_selector(target: ResourceTarget) -> dict[str, str]:
    """Exact Kustomize selector for ``target`` as the overlay's patches see it.

    Kustomize anchors selector regexes and matches a namespace-less resource as
    namespace ``default``; callers pass the identity before the overlay's own
    namespace and name transformers.
    """
    if "/" in target.api_version:
        group, version = target.api_version.split("/", 1)
    else:
        group, version = "", target.api_version
    if not version or "/" in version:
        raise KustomizePlanError("target apiVersion cannot be used in an explicit Kustomize selector")
    return {
        "group": re.escape(group) if group else "^$",
        "version": re.escape(version),
        "kind": re.escape(target.kind),
        "name": re.escape(target.name),
        "namespace": re.escape(target.namespace),
    }


def _add_patch_entry(
    config: MutableMapping[str, Any],
    original: str,
    overlay_path: str,
    patch_path: str,
    expected_selector: Mapping[str, str],
    root: Path,
    files: Mapping[str, Any],
) -> str:
    patches = config.get("patches")
    if patches is None:
        patches = CommentedSeq()
        config["patches"] = patches
    if not isinstance(patches, list):
        raise KustomizePlanError("Kustomization patches must be a list")
    expected_relative = posixpath.relpath(
        patch_path,
        "." if overlay_path == "." else overlay_path,
    )
    matching: list[Mapping[str, Any]] = []
    for entry in patches:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            continue
        if _resolve_reference(overlay_path, entry["path"]) == patch_path:
            matching.append(entry)
    if len(matching) > 1:
        raise KustomizePlanError("Kustomization contains duplicate entries for the managed GreenKube patch")
    if matching:
        if matching[0].get("target") != expected_selector or set(matching[0]) != {"path", "target"}:
            raise KustomizePlanError("existing GreenKube patch entry must have the exact explicit target selector")
        return original

    patch_file = CommentedMap()
    patch_file["path"] = expected_relative
    patch_file["target"] = CommentedMap(expected_selector)
    patches.append(patch_file)
    yaml = _roundtrip_yaml(original)
    stream = io.StringIO()
    try:
        yaml.dump(config, stream)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizePlanError("Kustomization could not be serialized deterministically") from exc
    updated = stream.getvalue()
    if original.endswith(("\n", "\r")) and not updated.endswith("\n"):
        updated += "\n"
    _validate_new_patch_path(root, files, overlay_path, patch_path)
    return updated


def _validate_new_patch_path(
    root: Path,
    files: Mapping[str, Any],
    overlay_path: str,
    patch_path: str,
) -> None:
    path = _canonical_repo_path(patch_path, allow_root=False)
    if path in files:
        return
    candidate = root.joinpath(*PurePosixPath(path).parts)
    current = root
    for part in PurePosixPath(path).parts:
        current = current / part
        if current.is_symlink():
            raise KustomizePlanError(f"{path}: new patch path must not traverse symbolic links")
        if current != candidate and current.exists() and not current.is_dir():
            raise KustomizePlanError(f"{current.relative_to(root).as_posix()}: patch parent must be a directory")
    if candidate.exists():
        raise KustomizePlanError(f"{path}: untracked workspace content conflicts with the new patch")
    if not _is_within_overlay(path, overlay_path):
        raise KustomizePlanError("new GreenKube patches must be created inside the resolved overlay")
    for existing in files:
        if path.startswith(f"{existing}/"):
            raise KustomizePlanError(f"{existing}: snapshot file conflicts with the new patch directory")


def _rendered_manifest(rendered: Any, target: ResourceTarget) -> Mapping[str, Any]:
    resource = next((item for item in rendered.resources if item.target == target), None)
    if resource is None or not isinstance(resource.manifest, Mapping):
        raise KustomizePlanError("rendered target workload disappeared before patch generation")
    return resource.manifest


def _patch_content(
    target: ResourceTarget,
    fields: Mapping[tuple[PathSegment, ...], Any],
    *,
    preserved_labels: Mapping[str, Any],
    preserved_annotations: Mapping[str, Any],
    preserved_fields: Mapping[tuple[PathSegment, ...], Any],
    rendered_target: Mapping[str, Any],
) -> str:
    """Render the managed strategic-merge patch for ``fields`` merged over ``preserved_fields``.

    Mapping keys are sorted; list entries start with their ``name`` key and follow the
    rendered target's list order (strategic merge keeps list order), unknown ones last.
    """
    patch = CommentedMap()
    patch["apiVersion"] = target.api_version
    patch["kind"] = target.kind
    metadata = CommentedMap()
    metadata["name"] = target.name
    metadata["namespace"] = target.namespace
    if preserved_labels:
        metadata["labels"] = CommentedMap(sorted(preserved_labels.items()))
    if preserved_annotations:
        metadata["annotations"] = CommentedMap(sorted(preserved_annotations.items()))
    patch["metadata"] = metadata

    tree: dict[PathSegment, Any] = {}
    for segments, value in {**preserved_fields, **fields}.items():
        node = tree
        for segment in segments[:-1]:
            node = node.setdefault(segment, {})
            if not isinstance(node, dict):
                raise KustomizePlanError(f"conflicting managed patch fields at {format_path(segments)}")
        if isinstance(node.get(segments[-1]), dict):
            raise KustomizePlanError(f"conflicting managed patch fields at {format_path(segments)}")
        node[segments[-1]] = value
    spec_tree = tree.pop("spec", None)
    if tree or not isinstance(spec_tree, dict):
        raise KustomizePlanError("managed patches may set fields under spec only")
    patch["spec"] = _patch_node(spec_tree, rendered_target.get("spec"))

    yaml = YAML(typ="rt", pure=True)
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4_096
    stream = io.StringIO()
    yaml.dump(patch, stream)
    content = stream.getvalue()
    return content if content.endswith("\n") else f"{content}\n"


def _patch_node(tree: Mapping[PathSegment, Any], rendered: Any) -> CommentedMap:
    node = CommentedMap()
    keys = [key for key in tree if isinstance(key, str)]
    selectors = [key for key in tree if isinstance(key, ListSelector)]
    if keys and selectors:
        raise KustomizePlanError("managed patch fields mix a mapping and a list at the same path")
    if selectors:
        raise KustomizePlanError("managed patch list entries must be under a named list field")
    for key in sorted(keys):
        value = tree[key]
        rendered_child = rendered.get(key) if isinstance(rendered, Mapping) else None
        if isinstance(value, dict) and value and all(isinstance(item, ListSelector) for item in value):
            node[key] = _patch_list(value, rendered_child)
        elif isinstance(value, dict):
            node[key] = _patch_node(value, rendered_child)
        else:
            node[key] = DoubleQuotedScalarString(value) if isinstance(value, str) else value
    return node


def _patch_list(entries: Mapping[ListSelector, Any], rendered: Any) -> CommentedSeq:
    rendered_items = [item for item in rendered if isinstance(item, Mapping)] if isinstance(rendered, list) else []
    rendered_names = [item.get(_PATCH_LIST_KEY) for item in rendered_items]
    if len(rendered_names) != len(set(map(str, rendered_names))):
        raise KustomizePlanError("rendered target workload has invalid container identities")
    by_name = {selector.value: (selector, value) for selector, value in entries.items()}
    ordered = [name for name in rendered_names if isinstance(name, str) and name in by_name]
    ordered.extend(sorted(set(by_name) - set(ordered)))
    sequence = CommentedSeq()
    for name in ordered:
        selector, value = by_name[name]
        rendered_item = next((item for item in rendered_items if item.get(_PATCH_LIST_KEY) == name), None)
        item = CommentedMap()
        item[selector.key] = name
        for key, child in _patch_node(value, rendered_item).items():
            item[key] = child
        sequence.append(item)
    return sequence


def _render(renderer: KustomizeRenderer, snapshot: RepositorySnapshot, overlay_path: str) -> RawRenderResult:
    result = renderer.render(snapshot, overlay_path)
    if (
        not isinstance(result, RawRenderResult)
        or result.renderer is not RendererKind.KUSTOMIZE
        or not isinstance(result.output_digest, str)
        or not SHA256_DIGEST.fullmatch(result.output_digest)
    ):
        raise KustomizePlanError("Kustomize renderer did not return a validated render result")
    return result


def _require_target(
    result: RawRenderResult,
    target: ResourceTarget,
    *,
    allow_namespace_less: bool = False,
) -> ResourceTarget:
    """Return the rendered identity of the live target, failing closed on ambiguity.

    The rendered workload matches when its identity equals the live one or, when
    ``allow_namespace_less`` is set, when it is namespace-less and the controller supplies
    the namespace (an Argo CD destination namespace), as for raw manifests.
    """
    identities = [_target_identity(resource.target) for resource in result.resources]
    if len(identities) != len(set(identities)):
        raise KustomizePlanError("Kustomize render contains ambiguous resource identities")
    matches = [resource.target for resource in result.resources if resource.target == target]
    if not matches and allow_namespace_less:
        matches = [
            resource.target for resource in result.resources if _rendered_target_matches(resource.target, target)
        ]
    if not matches:
        raise KustomizePlanError("no exact target resource was found in the resolved Kustomize overlay")
    if len(matches) != 1:
        raise KustomizePlanError("ambiguous exact target resource in the resolved Kustomize overlay")
    return matches[0]


def _render_as_deployed(
    renderer: KustomizeRenderer,
    snapshot: RepositorySnapshot,
    root: Path,
    snapshot_files: Mapping[str, Any],
    overlay_path: str,
    kustomization_path: str,
    kustomization_content: str,
    overrides: _FluxBuildOverrides,
    *,
    extra_files: Mapping[str, str] | None = None,
) -> RawRenderResult:
    """Render the overlay as the GitOps controller builds it.

    For a Flux Kustomization the root Kustomization is rendered with the namespace and
    name affixes that Flux writes into it before ``kustomize build``. These rewrites are
    staged for the render only and never become part of the planned Git change.
    """
    if overrides.rewrites_kustomization:
        kustomization_content = _rewrite_root_kustomization(
            kustomization_content,
            kustomization_path,
            namespace=overrides.target_namespace,
            name_prefix=overrides.name_prefix,
            name_suffix=overrides.name_suffix,
        )
    staged = dict(extra_files or {})
    if kustomization_content != _read_snapshot_file(root, snapshot_files, kustomization_path):
        staged[kustomization_path] = kustomization_content
    if not staged:
        return _render(renderer, snapshot, overlay_path)
    with _stage_files(root, snapshot_files, staged):
        return _render(renderer, snapshot, overlay_path)


_REMOVE = object()


def _rewrite_root_kustomization(
    content: str,
    path: str,
    *,
    namespace: Any = None,
    name_prefix: Any = None,
    name_suffix: Any = None,
) -> str:
    """Return ``content`` with root identity transformers set (a string) or removed (``_REMOVE``)."""
    yaml = _roundtrip_yaml(content)
    try:
        documents = list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizePlanError(f"{path}: Kustomization YAML is invalid") from exc
    if len(documents) != 1 or not isinstance(documents[0], MutableMapping):
        raise KustomizePlanError(f"{path}: Kustomization must contain exactly one mapping document")
    config = documents[0]
    for field_name, value in (("namespace", namespace), ("namePrefix", name_prefix), ("nameSuffix", name_suffix)):
        if value is _REMOVE:
            config.pop(field_name, None)
        elif value is not None:
            config[field_name] = value
    stream = io.StringIO()
    try:
        yaml.dump(config, stream)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizePlanError(f"{path}: Kustomization could not be serialized for render validation") from exc
    return stream.getvalue()


def _patch_selector_target(
    renderer: KustomizeRenderer,
    snapshot: RepositorySnapshot,
    root: Path,
    snapshot_files: Mapping[str, Any],
    overlay_path: str,
    kustomization_path: str,
    original_kustomization: str,
    config: Mapping[str, Any],
    overrides: _FluxBuildOverrides,
    rendered_target: ResourceTarget,
) -> ResourceTarget:
    """Identify the target as the overlay's own patches see it.

    Kustomize applies an overlay's patches before its namespace and namePrefix/nameSuffix
    transformers, so the patch selector must name the workload as it enters the overlay,
    not as it is deployed. When the overlay (or Flux) renames or re-namespaces resources,
    the overlay is rendered once more without those transformers and the workload must
    be found there exactly once.
    """
    namespace = overrides.target_namespace or _string_field(config, "namespace")
    prefix = overrides.name_prefix or _string_field(config, "namePrefix")
    suffix = overrides.name_suffix or _string_field(config, "nameSuffix")
    if not (namespace or prefix or suffix):
        return rendered_target.model_copy(update={"namespace": rendered_target.namespace or "default"})

    name = rendered_target.name
    if not name.startswith(prefix) or not name.endswith(suffix) or len(name) <= len(prefix) + len(suffix):
        raise KustomizePlanError(
            "the deployed workload name does not carry the overlay namePrefix/nameSuffix; "
            "GreenKube cannot identify it inside the overlay"
        )
    source_name = name[len(prefix) : len(name) - len(suffix)]
    probe_content = _rewrite_root_kustomization(
        original_kustomization,
        kustomization_path,
        namespace=_REMOVE,
        name_prefix=_REMOVE,
        name_suffix=_REMOVE,
    )
    probe = _render_as_deployed(
        renderer,
        snapshot,
        root,
        snapshot_files,
        overlay_path,
        kustomization_path,
        probe_content,
        _FluxBuildOverrides(),
    )
    candidates = [
        resource.target
        for resource in probe.resources
        if resource.target.api_version == rendered_target.api_version
        and resource.target.kind == rendered_target.kind
        and resource.target.name == source_name
        # Without a namespace transformer the namespace is unchanged and still discriminates.
        and (namespace or resource.target.namespace == rendered_target.namespace)
    ]
    if len(candidates) != 1:
        state = "is missing" if not candidates else "is ambiguous"
        raise KustomizePlanError(
            f"the target workload {state} before the overlay namespace/name transformers; "
            "GreenKube cannot write an exact patch selector"
        )
    return candidates[0].model_copy(update={"namespace": candidates[0].namespace or "default"})


def _string_field(config: Mapping[str, Any], field_name: str) -> str:
    value = config.get(field_name)
    return value if isinstance(value, str) else ""


_SUBSTITUTE_SWITCH = "kustomize.toolkit.fluxcd.io/substitute"


def _reject_post_build_variables(
    result: RawRenderResult,
    rendered_target: ResourceTarget,
    overrides: _FluxBuildOverrides,
    fields: Mapping[tuple[PathSegment, ...], Any],
) -> None:
    """Fail closed when Flux postBuild substitution can change what GreenKube validates.

    Flux substitutes variables in the built output, with values GreenKube cannot see
    (``substituteFrom`` ConfigMaps and Secrets). The workload identity, container names,
    container resources and every field the action edits must therefore be literal.
    Other fields are substituted identically before and after the patch and do not
    affect the plan.
    """
    if not overrides.post_build:
        return
    manifest = _resources_by_identity(result)[_target_identity(rendered_target)]
    metadata = manifest.get("metadata") if isinstance(manifest, Mapping) else None
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for values in (metadata.get("annotations"), metadata.get("labels")):
        if isinstance(values, Mapping) and values.get(_SUBSTITUTE_SWITCH) == "disabled":
            return
    checked: list[Any] = [metadata.get("name"), metadata.get("namespace")]
    checked.extend(read_field(manifest, format_path(segments))[1] for segments in fields)
    pod_spec: Any = manifest
    for field_name in POD_SPEC_PATHS.get(rendered_target.kind, ("spec",)):
        pod_spec = pod_spec.get(field_name) if isinstance(pod_spec, Mapping) else None
    if isinstance(pod_spec, Mapping):
        for list_name in ("initContainers", "containers"):
            containers = pod_spec.get(list_name)
            for container in containers if isinstance(containers, list) else []:
                if isinstance(container, Mapping):
                    checked.extend((container.get("name"), container.get("resources")))
    if "$" in json.dumps(checked, sort_keys=True, default=str):
        raise KustomizePlanError(
            "unsupported Flux postBuild substitution: the target workload's name, namespace, container names "
            "or container resources reference variables that GreenKube cannot resolve"
        )


def _validate_render_diff(
    before: RawRenderResult,
    after: RawRenderResult,
    target: ResourceTarget,
    workload_kind: str,
    operations: Sequence[TypedOperation],
) -> None:
    try:
        _validate_rendered_change(before, after, target, workload_kind, operations)
    except RawManifestPlanError as exc:
        raise KustomizePlanError(f"Kustomize render validation failed: {exc}") from exc


@contextmanager
def _stage_files(
    root: Path,
    snapshot_files: Mapping[str, Any],
    contents: Mapping[str, str],
) -> Iterator[None]:
    originals: dict[Path, bytes | None] = {}
    created_directories: list[Path] = []
    try:
        for relative_path, content in sorted(contents.items()):
            canonical = _canonical_repo_path(relative_path, allow_root=False)
            candidate = root.joinpath(*PurePosixPath(canonical).parts)
            if canonical in snapshot_files:
                originals[candidate] = _read_bytes(root, canonical)
            else:
                originals[candidate] = None
                parent = candidate.parent
                missing: list[Path] = []
                current = parent
                while current != root and not current.exists():
                    missing.append(current)
                    current = current.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_directories.append(directory)
            candidate.write_bytes(content.encode("utf-8"))
        yield
    except KustomizePlanError:
        raise
    except OSError as exc:
        raise KustomizePlanError("planned Kustomize files could not be staged for render validation") from exc
    finally:
        restore_error: OSError | None = None
        for path, original in reversed(tuple(originals.items())):
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError as exc:
                restore_error = restore_error or exc
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        if restore_error is not None:
            raise KustomizePlanError(
                "snapshot workspace could not be restored after render validation"
            ) from restore_error


def _read_bytes(root: Path, relative_path: str) -> bytes:
    path = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise KustomizePlanError(f"{relative_path}: snapshot file could not be read for staging") from exc


def _git_blob_sha(content: bytes, base_revision: str) -> str:
    algorithm = hashlib.sha256 if len(base_revision) == 64 else hashlib.sha1
    header = f"blob {len(content)}\0".encode("ascii")
    return algorithm(header + content).hexdigest()


def _file_diff(edits: Sequence[FileEdit], originals: Mapping[str, str]) -> str:
    chunks: list[str] = []
    for edit in sorted(edits, key=lambda item: item.path):
        old_content = originals.get(edit.path, "")
        new_content = edit.content or ""
        fromfile = "/dev/null" if edit.operation == "create" else f"a/{edit.path}"
        chunks.append(_unified_diff(old_content, new_content, fromfile, f"b/{edit.path}"))
    return "".join(chunks)
