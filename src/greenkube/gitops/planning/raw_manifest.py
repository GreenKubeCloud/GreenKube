"""Plan safe, per-container edits to raw Kubernetes manifests."""

from __future__ import annotations

import difflib
import hashlib
import io
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from greenkube.automation.actions import (
    ACTION_KINDS,
    ActionKindError,
    ActionKindSpec,
    PrimitiveApplyError,
    PrimitiveOp,
    RemoveField,
    SetField,
    Support,
)
from greenkube.automation.actions.container_resources import (
    container_operations,
    find_container,
    order_by_container,
    pod_spec,
    workload_containers,
)
from greenkube.automation.actions.validation import apply_action, validate_expected_delta
from greenkube.automation.actions.workloads import POD_SPEC_PATHS, WORKLOAD_API_VERSIONS, container_names
from greenkube.gitops.contracts import (
    ActionPlanV3,
    BindingStatus,
    FileEdit,
    FilePlan,
    RendererKind,
    ResourceTarget,
    SetContainerResources,
    TypedOperation,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.planning.digest import plan_digest
from greenkube.gitops.renderers import RawRenderError, RawRenderResult, RenderedResource, render_raw

_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
#: Compatibility aliases: the workload tables now live with the action kinds (H0).
_WORKLOAD_API_VERSIONS = WORKLOAD_API_VERSIONS
_POD_SPEC_PATHS = POD_SPEC_PATHS


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
    """Create a validated, deterministic FilePlan for a raw Kubernetes workload.

    This is the raw ``PatchStrategy``: the action kind compiles its typed operations to
    primitive field edits, which are applied to the round-trip source document; the
    rendered result is validated against the kind's expected delta.
    """

    renderer_kind = RendererKind.RAW

    def supports(self, op: PrimitiveOp, binding: WorkloadSource) -> Support:
        """Raw manifests express every field edit of the bound resource (H6/H3 add create and delete)."""
        if not isinstance(op, (SetField, RemoveField)):
            return Support.unsupported(f"raw manifest planning does not support {op.op!r} primitives yet")
        if op.ref != _binding_target(op.ref, binding):
            return Support.unsupported("raw manifest planning edits the bound resource only")
        return Support.supported()

    def plan(
        self,
        action: ActionPlanV3,
        source: WorkloadSource,
        snapshot: RepositorySnapshot,
        base_revision: str,
    ) -> FilePlan:
        spec, operations = self._validate_request(action, source, snapshot, base_revision)
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
            spec,
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
            raise RawManifestPlanError(f"the {spec.title} action does not change the target resource")

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
        digest = plan_digest(
            plan_id=plan_id,
            action=action,
            source=source,
            files=[file_edit],
            diff=diff,
            rendered_diff=rendered_diff,
            base_revision=base_revision,
            source_revision=source_revision,
            validation=validation,
            warnings=warnings,
            renderer=RendererKind.RAW,
            error_type=RawManifestPlanError,
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
    ) -> tuple[ActionKindSpec, list[TypedOperation]]:
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
        spec, operations = _action_kind(action, RawManifestPlanError)
        try:
            primitives = spec.compile(action.target, operations)
        except ActionKindError as exc:
            raise RawManifestPlanError(str(exc)) from exc
        planner = RawManifestPlanner()
        for primitive in primitives:
            support = planner.supports(primitive, source)
            if not support.ok:
                raise RawManifestPlanError(support.reason or "the action cannot be expressed in a raw manifest")
        return spec, operations


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
    spec: ActionKindSpec,
    operations: Sequence[TypedOperation],
) -> str:
    if PurePosixPath(path).suffix.lower() == ".json":
        document = _load_json(path, content)
        manifest = _matching_manifest(document, target)
        _apply_action(manifest, spec, target, operations)
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
    _apply_action(manifest, spec, target, operations)

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
            or (namespace == "" and target.namespace != "" and target.kind in POD_SPEC_PATHS)
        )
    )


def _binding_target(ref: ResourceTarget, binding: WorkloadSource) -> ResourceTarget:
    """The bound workload's identity, with the API version of ``ref`` (bindings carry no version)."""
    return ResourceTarget(
        api_version=ref.api_version,
        kind=binding.workload_kind,
        namespace=binding.namespace,
        name=binding.workload_name,
    )


def _action_kind(
    action: ActionPlanV3,
    error_type: type[ValueError],
) -> tuple[ActionKindSpec, list[TypedOperation]]:
    """The registered kind of ``action`` and its validated operations (errors become ``error_type``)."""
    operations: list[TypedOperation] = list(action.operations)
    try:
        spec = ACTION_KINDS.spec_for_action(action)
        spec.validate_operations(action.target, operations)
    except ActionKindError as exc:
        raise error_type(str(exc)) from exc
    return spec, operations


def _apply_action(
    manifest: MutableMapping[str, Any],
    spec: ActionKindSpec,
    target: ResourceTarget,
    operations: Sequence[TypedOperation],
) -> None:
    """Apply the kind's primitive field edits to a round-trip manifest and check its invariants."""
    try:
        apply_action(manifest, spec, target, operations, new_mapping=CommentedMap)
    except (ActionKindError, PrimitiveApplyError) as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _apply_operations(
    manifest: MutableMapping[str, Any],
    workload_kind: str,
    operations: Sequence[SetContainerResources],
) -> None:
    """Apply per-container resource operations (Helm expected-output checks; no invariant check)."""
    from greenkube.automation.actions import CONTAINER_RESOURCES
    from greenkube.automation.actions.primitives import apply_field_ops, field_operations

    target = ResourceTarget(api_version="v1", kind=workload_kind, namespace="", name="target")
    try:
        CONTAINER_RESOURCES.validate_target(manifest, target)
        primitives = CONTAINER_RESOURCES.compile(target, container_operations(operations))
        apply_field_ops(manifest, field_operations(primitives), new_mapping=CommentedMap)
    except (ActionKindError, PrimitiveApplyError) as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _validate_operation_resources(
    manifest: MutableMapping[str, Any],
    workload_kind: str,
    operations: Sequence[SetContainerResources],
) -> None:
    """The ``container_resources`` invariants (valid quantities, requests within limits)."""
    from greenkube.automation.actions import CONTAINER_RESOURCES

    target = ResourceTarget(api_version="v1", kind=workload_kind, namespace="", name="target")
    try:
        CONTAINER_RESOURCES.check_invariants(manifest, target, container_operations(operations))
    except ActionKindError as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _pod_spec(manifest: Mapping[str, Any], workload_kind: str) -> MutableMapping[str, Any]:
    try:
        return pod_spec(manifest, workload_kind)
    except ActionKindError as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _containers(spec: MutableMapping[str, Any]) -> list[MutableMapping[str, Any]]:
    try:
        return workload_containers(spec)
    except ActionKindError as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _find_container(containers: list[MutableMapping[str, Any]], name: str) -> MutableMapping[str, Any]:
    try:
        return find_container(containers, name)
    except ActionKindError as exc:
        raise RawManifestPlanError(str(exc)) from exc


def _validate_rendered_change(
    before: RawRenderResult,
    after: RawRenderResult,
    target: ResourceTarget,
    workload_kind: str,
    operations: Sequence[TypedOperation],
) -> None:
    """Validate the rendered change against the action kind's expected delta (H0c)."""
    del workload_kind
    try:
        spec = ACTION_KINDS.spec_for_operations(operations)
        validate_expected_delta(
            _resources_by_identity(before),
            _resources_by_identity(after),
            spec=spec,
            target=target,
            target_identity=_matching_target_identity(before, target),
            operations=operations,
        )
    except (ActionKindError, PrimitiveApplyError) as exc:
        raise RawManifestPlanError(str(exc)) from exc


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
            or (rendered.namespace == "" and target.namespace != "" and target.kind in POD_SPEC_PATHS)
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
    if manifest is None or target.kind not in POD_SPEC_PATHS:
        return None
    return container_names(manifest, target.kind)


def _order_operations_by_target_container(
    result: RawRenderResult,
    target: ResourceTarget,
    operations: Sequence[SetContainerResources],
) -> list[SetContainerResources]:
    """Order strategic-merge patch entries to preserve the rendered container list."""
    return order_by_container(operations, _target_container_order(result, target))


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


def _plan_identifier(action: ActionPlanV3, source: WorkloadSource, base_revision: str) -> str:
    """Keep previews idempotent per immutable source revision while allowing safe re-plans."""
    identity = {
        "action": action.model_dump(mode="json"),
        "base_revision": base_revision,
        "source_id": source.id,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"pln-{hashlib.sha256(encoded).hexdigest()[:24]}"
