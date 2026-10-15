"""Plan safe, per-container edits to raw Kubernetes manifests."""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    ContainerResources,
    FileEdit,
    FilePlan,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.renderers import RawRenderError, RawRenderResult, RenderedResource, render_raw

_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_WORKLOAD_API_VERSIONS = {
    "CronJob": frozenset({"batch/v1"}),
    "DaemonSet": frozenset({"apps/v1"}),
    "Deployment": frozenset({"apps/v1"}),
    "Job": frozenset({"batch/v1"}),
    "Pod": frozenset({"v1"}),
    "ReplicaSet": frozenset({"apps/v1"}),
    "ReplicationController": frozenset({"v1"}),
    "StatefulSet": frozenset({"apps/v1"}),
}
_POD_SPEC_PATHS = {
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Deployment": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "Pod": ("spec",),
    "ReplicaSet": ("spec", "template", "spec"),
    "ReplicationController": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
}
_QUANTITY_PATTERN = re.compile(
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<suffix>[eE][+-]?\d+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E)?"
)
_DECIMAL_SCALES = {
    "n": Fraction(1, 1_000_000_000),
    "u": Fraction(1, 1_000_000),
    "m": Fraction(1, 1_000),
    "k": Fraction(1_000),
    "K": Fraction(1_000),
    "M": Fraction(1_000_000),
    "G": Fraction(1_000_000_000),
    "T": Fraction(1_000_000_000_000),
    "P": Fraction(1_000_000_000_000_000),
    "E": Fraction(1_000_000_000_000_000_000),
}
_BINARY_SCALES = {
    "Ki": 1,
    "Mi": 2,
    "Gi": 3,
    "Ti": 4,
    "Pi": 5,
    "Ei": 6,
}
_MAX_KUBERNETES_QUANTITY = Fraction(2**63 - 1)


class RawManifestPlanError(ValueError):
    """The requested action cannot be applied safely to the raw source."""


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """An immutable view of repository file contents and their original blob SHAs."""

    contents: Mapping[str, str | bytes]
    original_blob_shas: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.contents, Mapping):
            raise RawManifestPlanError("snapshot contents must be a path-to-content mapping")
        if not isinstance(self.original_blob_shas, Mapping):
            raise RawManifestPlanError("snapshot blob SHAs must be a path-to-SHA mapping")

        copied_contents: dict[str, str | bytes] = {}
        for path, content in self.contents.items():
            _validate_snapshot_path(path)
            if not isinstance(content, (str, bytes)):
                raise RawManifestPlanError(f"{path}: snapshot content must be str or bytes")
            copied_contents[path] = content

        copied_shas: dict[str, str] = {}
        for path, blob_sha in self.original_blob_shas.items():
            _validate_snapshot_path(path)
            if path not in copied_contents:
                raise RawManifestPlanError(f"{path}: blob SHA has no corresponding snapshot content")
            if not isinstance(blob_sha, str) or not blob_sha.strip():
                raise RawManifestPlanError(f"{path}: original blob SHA must be a non-empty string")
            copied_shas[path] = blob_sha

        paths = set(copied_contents)
        for path in paths:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                parent = "/".join(parts[:depth])
                if parent in paths:
                    raise RawManifestPlanError(f"{parent!r} is both a file and a parent directory in the snapshot")

        object.__setattr__(self, "contents", MappingProxyType(copied_contents))
        object.__setattr__(self, "original_blob_shas", MappingProxyType(copied_shas))

    @property
    def blob_shas(self) -> Mapping[str, str]:
        """Compatibility alias for callers that use the shorter snapshot field name."""

        return self.original_blob_shas


class RawManifestPlanner:
    """Create a validated, deterministic FilePlan for a raw Kubernetes workload."""

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: RepositorySnapshot,
        base_revision: str,
    ) -> FilePlan:
        operations = self._validate_request(action, source, snapshot, base_revision)
        scope_sources = _sources_in_binding_scope(snapshot, source)
        before = _render(scope_sources)
        target_resources = [
            resource for resource in before.resources if _rendered_target_matches(resource.target, action.target)
        ]
        if not target_resources:
            raise RawManifestPlanError(
                f"no exact target resource was found in binding scope for {_target_description(action.target)}"
            )
        if len(target_resources) != 1:
            raise RawManifestPlanError(
                f"ambiguous exact target resource in binding scope for {_target_description(action.target)}"
            )

        target_resource = target_resources[0]
        target_path = target_resource.source_path
        original_sha = snapshot.original_blob_shas.get(target_path)
        if original_sha is None:
            raise RawManifestPlanError(f"{target_path}: original blob SHA is required for an update")
        original_content = _decode_content(target_path, snapshot.contents[target_path])
        updated_content = _update_source(
            target_path,
            original_content,
            target_resource,
            action.target,
            action.target.kind,
            operations,
        )

        updated_sources = dict(scope_sources)
        updated_sources[target_path] = updated_content
        after = _render(updated_sources)
        _validate_rendered_change(before, after, action.target, action.target.kind, operations)

        before_resources = _resources_by_identity(before)
        after_resources = _resources_by_identity(after)
        target_identity = _matching_target_identity(before, action.target)
        target_changed = before_resources[target_identity] != after_resources[target_identity]
        if not target_changed:
            raise RawManifestPlanError("the per-container rightsizing action does not change the target resource")

        diff = _unified_diff(original_content, updated_content, f"a/{target_path}", f"b/{target_path}")
        rendered_diff = _unified_diff(
            _rendered_json(before),
            _rendered_json(after),
            "rendered-before",
            "rendered-after",
        )
        warnings = sorted(set(before.warnings + after.warnings))
        source_revision = source.source_revision or source.revision
        file_edit = FileEdit(
            path=target_path,
            operation="update",
            original_sha=original_sha,
            content=updated_content,
        )
        validation = ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest=after.output_digest,
            warnings=warnings,
        )
        plan_id = _plan_identifier(action, source, base_revision)
        digest = _plan_digest(
            plan_id=plan_id,
            action=action,
            source=source,
            file_edit=file_edit,
            diff=diff,
            rendered_diff=rendered_diff,
            base_revision=base_revision,
            source_revision=source_revision,
            validation=validation,
            warnings=warnings,
        )
        return FilePlan(
            plan_id=plan_id,
            files=[file_edit],
            diff=diff,
            rendered_diff=rendered_diff,
            base_revision=base_revision,
            source_revision=source_revision,
            renderer=RendererKind.RAW,
            validation=validation,
            warnings=warnings,
            digest=digest,
        )

    @staticmethod
    def _validate_request(
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: RepositorySnapshot,
        base_revision: str,
    ) -> list[SetContainerResources]:
        if not isinstance(snapshot, RepositorySnapshot):
            raise RawManifestPlanError("snapshot must be an immutable RepositorySnapshot")
        if not isinstance(base_revision, str) or not base_revision.strip():
            raise RawManifestPlanError("resolved base revision must be a non-empty string")
        if source.renderer is not RendererKind.RAW:
            raise RawManifestPlanError("source must use the raw renderer")
        if source.status is not BindingStatus.RESOLVED:
            raise RawManifestPlanError("workload source binding must be resolved")
        if (
            action.target.kind != source.workload_kind
            or action.target.namespace != source.namespace
            or action.target.name != source.workload_name
        ):
            raise RawManifestPlanError("action target must match the resolved workload binding")

        supported_versions = _WORKLOAD_API_VERSIONS.get(action.target.kind)
        if supported_versions is None or action.target.api_version not in supported_versions:
            raise RawManifestPlanError(
                f"unsupported workload kind or API version: {action.target.api_version}/{action.target.kind}"
            )
        if not action.operations or any(not isinstance(op, SetContainerResources) for op in action.operations):
            raise RawManifestPlanError("only per-container rightsizing operations are supported")

        operations = [operation for operation in action.operations if isinstance(operation, SetContainerResources)]
        container_names = [operation.container for operation in operations]
        if len(container_names) != len(set(container_names)):
            raise RawManifestPlanError("each container may have only one per-container rightsizing operation")
        for operation in operations:
            resources = operation.resources
            if not resources.requests and not resources.limits:
                raise RawManifestPlanError("a rightsizing operation must set at least one resource quantity")
        return operations


def plan_raw_manifest(
    action: ActionPlanV3,
    source: WorkloadSource,
    snapshot: RepositorySnapshot,
    base_revision: str,
) -> FilePlan:
    """Plan one raw-manifest action without mutating the supplied snapshot."""

    return RawManifestPlanner().plan(action, source, snapshot, base_revision)


def _validate_snapshot_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise RawManifestPlanError(f"snapshot path {path!r} must be an unambiguous relative POSIX path")


def _sources_in_binding_scope(
    snapshot: RepositorySnapshot,
    source: WorkloadSource,
) -> dict[str, str | bytes]:
    scope = source.path
    sources = {
        path: content
        for path, content in snapshot.contents.items()
        if _is_in_scope(path, scope) and PurePosixPath(path).suffix.lower() in _MANIFEST_SUFFIXES
    }
    if not sources:
        raise RawManifestPlanError("no exact target resource was found in binding scope")
    return dict(sorted(sources.items()))


def _is_in_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(f"{scope}/")


def _render(sources: Mapping[str, str | bytes]) -> RawRenderResult:
    try:
        return render_raw(sources)
    except RawRenderError as exc:
        detail = str(exc)
        if "duplicate resource identity" in detail:
            raise RawManifestPlanError(f"ambiguous raw source: {detail}") from exc
        raise RawManifestPlanError(f"raw render validation failed: {detail}") from exc


def _decode_content(path: str, content: str | bytes) -> str:
    if isinstance(content, str):
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RawManifestPlanError(f"{path}: source content must be valid UTF-8") from exc
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RawManifestPlanError(f"{path}: source content must be valid UTF-8") from exc


def _update_source(
    path: str,
    content: str,
    target_resource: RenderedResource,
    target: ResourceTarget,
    workload_kind: str,
    operations: list[SetContainerResources],
) -> str:
    if PurePosixPath(path).suffix.lower() == ".json":
        document = _load_json(path, content)
        manifest = _matching_manifest(document, target)
        _apply_operations(manifest, workload_kind, operations)
        _validate_operation_resources(manifest, workload_kind, operations)
        return _dump_json(content, document)

    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4_096
    if "\r\n" in content and "\n" not in content.replace("\r\n", ""):
        yaml.line_break = "\r\n"
    try:
        documents = list(yaml.load_all(content))
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise RawManifestPlanError(f"{path}: round-trip YAML parsing failed") from exc
    if target_resource.document_index >= len(documents):
        raise RawManifestPlanError(f"{path}: rendered document index is absent from the round-trip source")

    manifest = _matching_manifest(documents[target_resource.document_index], target)
    _apply_operations(manifest, workload_kind, operations)
    _validate_operation_resources(manifest, workload_kind, operations)

    stream = io.StringIO()
    try:
        yaml.dump_all(documents, stream)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise RawManifestPlanError(f"{path}: round-trip YAML serialization failed") from exc
    updated = stream.getvalue()
    if content.endswith(("\n", "\r")):
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = updated.rstrip("\r\n")
    return updated


def _load_json(path: str, content: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RawManifestPlanError(f"{path}: duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(content, object_pairs_hook=unique_object)
    except RawManifestPlanError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError, TypeError) as exc:
        raise RawManifestPlanError(f"{path}: round-trip JSON parsing failed") from exc


def _dump_json(original: str, document: Any) -> str:
    indent = _json_indent(original)
    if indent is None:
        separators = (", ", ": ") if ": " in original or ", " in original else (",", ":")
        rendered = json.dumps(document, ensure_ascii=False, separators=separators, allow_nan=False)
    else:
        rendered = json.dumps(document, ensure_ascii=False, indent=indent, allow_nan=False)
    if "\r\n" in original:
        rendered = rendered.replace("\n", "\r\n")
    if original.endswith(("\n", "\r")):
        newline = "\r\n" if "\r\n" in original else "\n"
        rendered += newline
    return rendered


def _json_indent(content: str) -> int | str | None:
    prefixes: list[str] = []
    for line in content.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            prefixes.append(line[: len(line) - len(stripped)])
    if not prefixes:
        return None
    indent_unit = min(prefixes, key=len)
    if set(indent_unit) == {" "}:
        return len(indent_unit)
    if set(indent_unit) == {"\t"}:
        return indent_unit
    return 2


def _matching_manifest(document: Any, target: ResourceTarget) -> MutableMapping[str, Any]:
    if not isinstance(document, Mapping):
        raise RawManifestPlanError("target document must be a Kubernetes resource mapping")
    if document.get("kind") == "List":
        items = document.get("items")
        if not isinstance(items, list):
            raise RawManifestPlanError("kind: List requires an items list")
        candidates = items
    else:
        candidates = [document]

    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, MutableMapping) and _matches_target(candidate, target)
    ]
    if len(matches) != 1:
        raise RawManifestPlanError(f"document must contain exactly one resource matching {_target_description(target)}")
    return matches[0]


def _matches_target(manifest: Mapping[str, Any], target: ResourceTarget) -> bool:
    metadata = manifest.get("metadata")
    namespace = metadata.get("namespace", "") if isinstance(metadata, Mapping) else None
    return (
        manifest.get("apiVersion") == target.api_version
        and manifest.get("kind") == target.kind
        and isinstance(metadata, Mapping)
        and metadata.get("name") == target.name
        and (
            namespace == target.namespace
            or (namespace == "" and target.namespace != "" and target.kind in _POD_SPEC_PATHS)
        )
    )


def _apply_operations(
    manifest: MutableMapping[str, Any],
    workload_kind: str,
    operations: list[SetContainerResources],
) -> None:
    pod_spec = _pod_spec(manifest, workload_kind)
    containers = pod_spec.get("containers")
    if not isinstance(containers, list):
        raise RawManifestPlanError(f"{workload_kind} target must contain a containers list")

    for operation in operations:
        matches: list[MutableMapping[str, Any]] = []
        for container in containers:
            if not isinstance(container, MutableMapping):
                raise RawManifestPlanError("workload containers must be mappings")
            name = container.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RawManifestPlanError("workload containers must have non-empty names")
            if name == operation.container:
                matches.append(container)
        if len(matches) != 1:
            raise RawManifestPlanError(f"container {operation.container!r} must exist exactly once in the workload")

        container = matches[0]
        if "resources" not in container:
            resources: MutableMapping[str, Any] = CommentedMap()
            container["resources"] = resources
        else:
            resources_value = container["resources"]
            if not isinstance(resources_value, MutableMapping):
                raise RawManifestPlanError(f"container {operation.container!r} resources must be a mapping")
            resources = resources_value

        for field_name, values in (
            ("requests", operation.resources.requests),
            ("limits", operation.resources.limits),
        ):
            if not values:
                continue
            if field_name not in resources:
                section: MutableMapping[str, Any] = CommentedMap()
                resources[field_name] = section
            else:
                section_value = resources[field_name]
                if not isinstance(section_value, MutableMapping):
                    raise RawManifestPlanError(
                        f"container {operation.container!r} resources.{field_name} must be a mapping"
                    )
                section = section_value
            for resource_name, quantity in values.items():
                section[resource_name] = quantity


def _validate_operation_resources(
    manifest: MutableMapping[str, Any],
    workload_kind: str,
    operations: list[SetContainerResources],
) -> None:
    containers = _containers(_pod_spec(manifest, workload_kind))
    for operation in operations:
        container = _find_container(containers, operation.container)
        raw_resources = container.get("resources", {})
        if not isinstance(raw_resources, Mapping):
            raise RawManifestPlanError(f"container {operation.container!r} resources must be a mapping")
        try:
            resources = ContainerResources(
                requests=dict(raw_resources.get("requests", {})),
                limits=dict(raw_resources.get("limits", {})),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise RawManifestPlanError(
                f"container {operation.container!r} has an invalid Kubernetes resource quantity or mapping"
            ) from exc

        request_values = {
            resource_name: _quantity_value(quantity) for resource_name, quantity in resources.requests.items()
        }
        limit_values = {
            resource_name: _quantity_value(quantity) for resource_name, quantity in resources.limits.items()
        }
        for resource_name, request in resources.requests.items():
            limit = limit_values.get(resource_name)
            if limit is not None and request_values[resource_name] > limit:
                raise RawManifestPlanError(
                    f"container {operation.container!r} request for {resource_name!r} exceeds its limit"
                )


def _pod_spec(manifest: Mapping[str, Any], workload_kind: str) -> MutableMapping[str, Any]:
    path = _POD_SPEC_PATHS.get(workload_kind)
    if path is None:
        raise RawManifestPlanError(f"unsupported workload kind: {workload_kind}")
    current: Any = manifest
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            raise RawManifestPlanError(f"{workload_kind} target is missing its pod specification")
        current = current[part]
    if not isinstance(current, MutableMapping):
        raise RawManifestPlanError(f"{workload_kind} pod specification must be a mapping")
    return current


def _containers(pod_spec: MutableMapping[str, Any]) -> list[MutableMapping[str, Any]]:
    values = pod_spec.get("containers")
    if not isinstance(values, list):
        raise RawManifestPlanError("workload target must contain a containers list")
    if any(not isinstance(value, MutableMapping) for value in values):
        raise RawManifestPlanError("workload containers must be mappings")
    return values


def _find_container(
    containers: list[MutableMapping[str, Any]],
    name: str,
) -> MutableMapping[str, Any]:
    matches = [container for container in containers if container.get("name") == name]
    if len(matches) != 1:
        raise RawManifestPlanError(f"container {name!r} must exist exactly once in the workload")
    return matches[0]


def _quantity_value(quantity: str) -> Fraction:
    if len(quantity) > 128:
        raise RawManifestPlanError("Kubernetes resource quantity exceeds the supported precision")
    match = _QUANTITY_PATTERN.fullmatch(quantity)
    if match is None:
        raise RawManifestPlanError(f"invalid Kubernetes resource quantity {quantity!r}")

    number = match.group("number")
    suffix = match.group("suffix") or ""
    try:
        if re.fullmatch(r"[eE][+-]?\d+", suffix):
            exponent = int(suffix[1:])
            if abs(exponent) > 1_000:
                raise RawManifestPlanError("Kubernetes resource quantity exponent is out of range")
            value = Fraction(Decimal(f"{number}{suffix}"))
        else:
            value = Fraction(Decimal(number))
            if suffix in _DECIMAL_SCALES:
                value *= _DECIMAL_SCALES[suffix]
            elif suffix in _BINARY_SCALES:
                value *= 1_024 ** _BINARY_SCALES[suffix]
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise RawManifestPlanError(f"invalid Kubernetes resource quantity {quantity!r}") from exc

    if value < 0 or value > _MAX_KUBERNETES_QUANTITY:
        raise RawManifestPlanError(f"Kubernetes resource quantity {quantity!r} is outside the supported range")
    return value


def _validate_rendered_change(
    before: RawRenderResult,
    after: RawRenderResult,
    target: ResourceTarget,
    workload_kind: str,
    operations: list[SetContainerResources],
) -> None:
    before_resources = _resources_by_identity(before)
    after_resources = _resources_by_identity(after)
    target_identity = _matching_target_identity(before, target)
    if target_identity not in before_resources or target_identity not in after_resources:
        raise RawManifestPlanError("render validation did not preserve the exact target resource")
    if before_resources.keys() != after_resources.keys():
        raise RawManifestPlanError("render validation found added or removed resources")

    for identity, manifest in before_resources.items():
        if identity != target_identity and manifest != after_resources[identity]:
            raise RawManifestPlanError("render validation found an unrelated resource change")

    expected_target = copy.deepcopy(before_resources[target_identity])
    if not isinstance(expected_target, MutableMapping):
        raise RawManifestPlanError("rendered target is not a mutable resource mapping")
    _apply_operations(expected_target, workload_kind, operations)
    _validate_operation_resources(expected_target, workload_kind, operations)
    expected_comparison, expected_checksums = _normalize_checksum_annotations(expected_target, workload_kind)
    actual_comparison, actual_checksums = _normalize_checksum_annotations(
        after_resources[target_identity],
        workload_kind,
    )
    if expected_checksums != actual_checksums or expected_comparison != actual_comparison:
        raise RawManifestPlanError("render validation found changes outside the requested container resources")
    _validate_operation_resources(after_resources[target_identity], workload_kind, operations)


def _normalize_checksum_annotations(
    manifest: Mapping[str, Any],
    workload_kind: str,
) -> tuple[dict[str, Any], frozenset[str]]:
    comparison = copy.deepcopy(dict(manifest))
    pod_spec_path = _POD_SPEC_PATHS.get(workload_kind)
    if pod_spec_path is None:
        return comparison, frozenset()
    metadata_path = ("metadata",) if workload_kind == "Pod" else (*pod_spec_path[:-1], "metadata")
    parent: MutableMapping[str, Any] = comparison
    for part in metadata_path:
        value = parent.get(part)
        if not isinstance(value, MutableMapping):
            return comparison, frozenset()
        parent = value
    annotations = parent.get("annotations")
    if not isinstance(annotations, MutableMapping):
        return comparison, frozenset()

    checksum_keys = frozenset(key for key in annotations if isinstance(key, str) and key.startswith("checksum/"))
    for key in checksum_keys:
        annotations[key] = "<rendered-checksum>"
    return comparison, checksum_keys


def _resources_by_identity(result: RawRenderResult) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    return {_target_identity(resource.target): resource.manifest for resource in result.resources}


def _target_identity(target: ResourceTarget) -> tuple[str, str, str, str]:
    return target.api_version, target.kind, target.namespace, target.name


def _rendered_target_matches(rendered: ResourceTarget, target: ResourceTarget) -> bool:
    return (
        rendered.api_version == target.api_version
        and rendered.kind == target.kind
        and rendered.name == target.name
        and (
            rendered.namespace == target.namespace
            or (rendered.namespace == "" and target.namespace != "" and target.kind in _POD_SPEC_PATHS)
        )
    )


def _matching_target_identity(
    result: RawRenderResult,
    target: ResourceTarget,
) -> tuple[str, str, str, str]:
    matches = [resource.target for resource in result.resources if _rendered_target_matches(resource.target, target)]
    if len(matches) != 1:
        raise RawManifestPlanError(
            f"rendered source must contain exactly one target workload for {_target_description(target)}"
        )
    return _target_identity(matches[0])


def _target_container_order(result: RawRenderResult, target: ResourceTarget) -> tuple[str, ...] | None:
    manifest = _resources_by_identity(result).get(_matching_target_identity(result, target))
    pod_spec_path = _POD_SPEC_PATHS.get(target.kind)
    if manifest is None or pod_spec_path is None:
        return None

    current: Any = manifest
    for field_name in pod_spec_path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(field_name)
    if not isinstance(current, Mapping):
        return None
    containers = current.get("containers")
    if not isinstance(containers, list):
        return None

    names: list[str] = []
    for container in containers:
        if not isinstance(container, Mapping):
            return None
        name = container.get("name")
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return tuple(names) if len(names) == len(set(names)) else None


def _order_operations_by_target_container(
    result: RawRenderResult,
    target: ResourceTarget,
    operations: Sequence[SetContainerResources],
) -> list[SetContainerResources]:
    """Order strategic-merge patch entries to preserve the rendered container list."""
    container_order = _target_container_order(result, target)
    if container_order is None:
        return list(operations)
    positions = {name: index for index, name in enumerate(container_order)}
    return sorted(
        operations,
        key=lambda operation: (positions.get(operation.container, len(positions)), operation.container),
    )


def _target_description(target: ResourceTarget) -> str:
    return (
        f"apiVersion={target.api_version!r}, kind={target.kind!r}, namespace={target.namespace!r}, name={target.name!r}"
    )


def _rendered_json(result: RawRenderResult) -> str:
    resources = sorted(
        result.resources,
        key=lambda resource: _target_identity(resource.target),
    )
    payload = [
        {
            "identity": {
                "api_version": resource.target.api_version,
                "kind": resource.target.kind,
                "namespace": resource.target.namespace,
                "name": resource.target.name,
            },
            "manifest": resource.manifest,
        }
        for resource in resources
    ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _unified_diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="\n",
        )
    )


def _plan_digest(
    *,
    plan_id: str,
    action: ActionPlanV3,
    source: WorkloadSource,
    file_edit: FileEdit,
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
        "files": [file_edit.model_dump(mode="json")],
        "diff": diff,
        "rendered_diff": rendered_diff,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "renderer": RendererKind.RAW.value,
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
        raise RawManifestPlanError("plan inputs must be deterministically JSON-serializable") from exc
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _plan_identifier(action: ActionPlanV3, source: WorkloadSource, base_revision: str) -> str:
    """Keep previews idempotent per immutable source revision while allowing safe re-plans."""
    identity = {
        "action": action.model_dump(mode="json"),
        "base_revision": base_revision,
        "source_id": source.id,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"pln-{hashlib.sha256(encoded).hexdigest()[:24]}"
