"""Plan safe, overlay-local Kustomize strategic patches."""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    FileEdit,
    FilePlan,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.planning.raw_manifest import (
    _POD_SPEC_PATHS,
    _WORKLOAD_API_VERSIONS,
    RawManifestPlanError,
    _order_operations_by_target_container,
    _plan_identifier,
    _rendered_json,
    _resources_by_identity,
    _target_identity,
    _unified_diff,
    _validate_rendered_change,
)
from greenkube.gitops.renderers.kustomize import KustomizeRenderer
from greenkube.gitops.renderers.raw import RawRenderResult
from greenkube.gitops.snapshots.models import RepositorySnapshot

KUSTOMIZE_VERSION = "v5.6.0"

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
_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SNAPSHOT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATCH_ENTRY_KEYS = frozenset({"path", "patch", "target", "options"})
_PATCH_TARGET_KEYS = frozenset({"group", "version", "kind", "name", "namespace", "labelSelector", "annotationSelector"})
_PATCH_METADATA_MARKERS = frozenset({"greenkube.cloud/managed", "greenkube.cloud/action-id"})


class KustomizePlanError(ValueError):
    """The requested action cannot be applied safely to a Kustomize overlay."""


class KustomizePlanner:
    """Create deterministic, render-validated FilePlans for one resolved overlay."""

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
        operations = _validate_request(action, source, snapshot, base_revision)
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
        _validate_target_namespace(source, config)
        before = _render(self.renderer, snapshot, overlay_path)
        _require_exact_target(before, action.target)

        expected_path = _managed_patch_path(overlay_path, action.target)
        patch_path, preserved_labels, preserved_annotations, preserved_requests = _find_managed_patch(
            root,
            snapshot_files,
            overlay_path,
            kustomization_path,
            config,
            expected_path,
            action.target,
        )
        patch_content = _patch_content(
            action.target,
            _order_operations_by_target_container(before, action.target, operations),
            preserved_labels=preserved_labels,
            preserved_annotations=preserved_annotations,
            preserved_requests=preserved_requests,
            container_order=_target_container_order(before, action.target),
        )
        original_kustomization = _read_snapshot_file(root, snapshot_files, kustomization_path)
        updated_kustomization = _add_patch_entry(
            config,
            original_kustomization,
            overlay_path,
            patch_path,
            action.target,
            root,
            snapshot_files,
        )

        original_patch = snapshot_files.get(patch_path)
        if original_patch is not None:
            original_patch_content = _read_snapshot_file(root, snapshot_files, patch_path)
        else:
            original_patch_content = None

        staged_files = {
            kustomization_path: updated_kustomization,
            patch_path: patch_content,
        }
        with _stage_files(root, snapshot_files, staged_files):
            after = _render(self.renderer, snapshot, overlay_path)

        _validate_render_diff(before, after, action.target, action.target.kind, operations)
        before_resources = _resources_by_identity(before)
        after_resources = _resources_by_identity(after)
        target_identity = _target_identity(action.target)
        target_changed = before_resources[target_identity] != after_resources[target_identity]
        if not target_changed:
            raise KustomizePlanError("the requested container requests are already present in the rendered target")

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


def _validate_request(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: RepositorySnapshot,
    base_revision: str,
) -> list[SetContainerResources]:
    if not isinstance(snapshot, RepositorySnapshot):
        raise KustomizePlanError("snapshot must be an archive RepositorySnapshot")
    if not isinstance(base_revision, str) or not _SHA_PATTERN.fullmatch(base_revision):
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
    supported_versions = _WORKLOAD_API_VERSIONS.get(action.target.kind)
    if supported_versions is None or action.target.api_version not in supported_versions:
        raise KustomizePlanError(
            f"unsupported workload kind or API version: {action.target.api_version}/{action.target.kind}"
        )
    if not action.operations or any(
        not isinstance(operation, SetContainerResources) for operation in action.operations
    ):
        raise KustomizePlanError("only per-container rightsizing operations are supported")

    operations = [operation for operation in action.operations if isinstance(operation, SetContainerResources)]
    if len({operation.container for operation in operations}) != len(operations):
        raise KustomizePlanError("each container may have only one per-container rightsizing operation")
    for operation in operations:
        if not operation.resources.requests:
            raise KustomizePlanError("each operation must set at least one container request")
        if operation.resources.limits:
            raise KustomizePlanError("Kustomize v1 supports requests only; limits edits are unsupported")
    _validate_renderer_config(source)
    return operations


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
            raise KustomizePlanError(f"renderer override {feature!r} is not supported")
    base_paths = config.get("base_paths", [])
    if base_paths is None:
        base_paths = []
    if not isinstance(base_paths, list):
        raise KustomizePlanError("renderer base_paths must be a list of snapshot-relative paths")
    for path in base_paths:
        _canonical_repo_path(path, allow_root=True)
    if "overlay_path" in config and config["overlay_path"] != source.path:
        raise KustomizePlanError("renderer overlay_path must match the resolved workload source path")


def _validate_target_namespace(source: WorkloadSource, config: Mapping[str, Any]) -> None:
    target_namespace = source.renderer_config.get("target_namespace")
    if target_namespace is None:
        return
    if not isinstance(target_namespace, str) or target_namespace != source.namespace:
        raise KustomizePlanError("unsupported Flux targetNamespace: it must match the resolved workload namespace")
    if config.get("namespace") != target_namespace:
        raise KustomizePlanError("unsupported Flux targetNamespace: it must match the overlay namespace")


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
        if not isinstance(item.digest, str) or not _SNAPSHOT_DIGEST_PATTERN.fullmatch(item.digest):
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
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
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

    matches: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]] = []
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
        labels, annotations, requests = _validate_managed_patch(parsed, path, target)
        if path in referenced:
            entry = referenced[path]
            expected_selector = _target_selector(target)
            if entry.get("target") != expected_selector or set(entry) != {"path", "target"}:
                raise KustomizePlanError(f"{kustomization_path}: managed patch requires an exact target selector")
        matches.append((path, labels, annotations, requests))

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
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
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
    current: Any = spec
    pod_path = _POD_SPEC_PATHS[target.kind]
    for field_name in pod_path[1:]:
        if not isinstance(current, Mapping) or set(current) != {field_name}:
            raise KustomizePlanError(f"{path}: managed patch must contain only container requests")
        current = current[field_name]
    if not isinstance(current, Mapping) or set(current) != {"containers"}:
        raise KustomizePlanError(f"{path}: managed patch must contain only container requests")
    containers = current["containers"]
    if not isinstance(containers, list) or not containers:
        raise KustomizePlanError(f"{path}: managed patch containers must be a non-empty list")
    preserved_requests: dict[str, dict[str, str]] = {}
    for container in containers:
        if not isinstance(container, Mapping) or set(container) != {"name", "resources"}:
            raise KustomizePlanError(f"{path}: managed patch containers may include requests only")
        name = container.get("name")
        resources = container.get("resources")
        if not isinstance(name, str) or not name or name in preserved_requests or not isinstance(resources, Mapping):
            raise KustomizePlanError(f"{path}: managed patch container names are invalid")
        requests = resources.get("requests")
        if set(resources) != {"requests"} or not isinstance(requests, Mapping):
            raise KustomizePlanError(f"{path}: managed patch resources may include requests only")
        if (
            not requests
            or set(requests) - {"cpu", "memory"}
            or any(
                not isinstance(quantity, str) or not quantity or quantity != quantity.strip()
                for quantity in requests.values()
            )
        ):
            raise KustomizePlanError(f"{path}: managed patch requests must contain valid CPU or memory values")
        preserved_requests[name] = dict(requests)
    return dict(labels), dict(annotations), preserved_requests


def _target_selector(target: ResourceTarget) -> dict[str, str]:
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
    target: ResourceTarget,
    root: Path,
    files: Mapping[str, Any],
) -> str:
    patches = config.get("patches")
    if patches is None:
        patches = CommentedSeq()
        config["patches"] = patches
    if not isinstance(patches, list):
        raise KustomizePlanError("Kustomization patches must be a list")
    expected_selector = _target_selector(target)
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


def _target_container_order(rendered: Any, target: ResourceTarget) -> tuple[str, ...]:
    resource = next((item for item in rendered.resources if item.target == target), None)
    if resource is None:
        raise KustomizePlanError("rendered target workload disappeared before patch generation")
    current: Any = resource.manifest
    for field_name in _POD_SPEC_PATHS[target.kind]:
        if not isinstance(current, Mapping):
            raise KustomizePlanError("rendered target workload has an invalid pod spec")
        current = current.get(field_name)
    if not isinstance(current, Mapping) or not isinstance(current.get("containers"), list):
        raise KustomizePlanError("rendered target workload has an invalid container list")
    names = tuple(
        container["name"]
        for container in current["containers"]
        if isinstance(container, Mapping) and isinstance(container.get("name"), str)
    )
    if not names or len(names) != len(set(names)):
        raise KustomizePlanError("rendered target workload has invalid container identities")
    return names


def _patch_content(
    target: ResourceTarget,
    operations: list[SetContainerResources],
    *,
    preserved_labels: Mapping[str, Any],
    preserved_annotations: Mapping[str, Any],
    preserved_requests: Mapping[str, Mapping[str, str]],
    container_order: Sequence[str],
) -> str:
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

    current: MutableMapping[str, Any] = patch
    for field_name in _POD_SPEC_PATHS[target.kind]:
        nested = CommentedMap()
        current[field_name] = nested
        current = nested
    containers = CommentedSeq()
    current["containers"] = containers
    operations_by_container = {operation.container: operation for operation in operations}
    container_names = set(preserved_requests) | set(operations_by_container)
    ordered_names = [name for name in container_order if name in container_names]
    ordered_names.extend(sorted(container_names - set(ordered_names)))
    for name in ordered_names:
        operation = operations_by_container.get(name)
        requests_data = dict(preserved_requests.get(name, {}))
        if operation is not None:
            requests_data.update(operation.resources.requests)
        container = CommentedMap()
        container["name"] = name
        resources = CommentedMap()
        requests = CommentedMap()
        for resource_name, quantity in sorted(requests_data.items()):
            requests[resource_name] = DoubleQuotedScalarString(quantity)
        resources["requests"] = requests
        container["resources"] = resources
        containers.append(container)

    yaml = YAML(typ="rt", pure=True)
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4_096
    stream = io.StringIO()
    yaml.dump(patch, stream)
    content = stream.getvalue()
    return content if content.endswith("\n") else f"{content}\n"


def _render(renderer: KustomizeRenderer, snapshot: RepositorySnapshot, overlay_path: str) -> RawRenderResult:
    result = renderer.render(snapshot, overlay_path)
    if (
        not isinstance(result, RawRenderResult)
        or result.renderer is not RendererKind.KUSTOMIZE
        or not isinstance(result.output_digest, str)
        or not _SNAPSHOT_DIGEST_PATTERN.fullmatch(result.output_digest)
    ):
        raise KustomizePlanError("Kustomize renderer did not return a validated render result")
    return result


def _require_exact_target(result: RawRenderResult, target: ResourceTarget) -> None:
    identities = [_target_identity(resource.target) for resource in result.resources]
    if len(identities) != len(set(identities)):
        raise KustomizePlanError("Kustomize render contains ambiguous resource identities")
    matches = [resource for resource in result.resources if resource.target == target]
    if not matches:
        raise KustomizePlanError("no exact target resource was found in the resolved Kustomize overlay")
    if len(matches) != 1:
        raise KustomizePlanError("ambiguous exact target resource in the resolved Kustomize overlay")


def _validate_render_diff(
    before: RawRenderResult,
    after: RawRenderResult,
    target: ResourceTarget,
    workload_kind: str,
    operations: list[SetContainerResources],
) -> None:
    try:
        _validate_rendered_change(before, after, target, workload_kind, operations)
    except RawManifestPlanError as exc:
        raise KustomizePlanError(f"Kustomize render validation failed: {exc}") from exc
    _require_exact_target(after, target)


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
        "files": [edit.model_dump(mode="json") for edit in sorted(edits, key=lambda item: item.path)],
        "diff": diff,
        "rendered_diff": rendered_diff,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "renderer": RendererKind.KUSTOMIZE.value,
        "validation": validation.model_dump(mode="json"),
        "warnings": warnings,
    }
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise KustomizePlanError("plan inputs must be deterministically JSON-serializable") from exc
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
