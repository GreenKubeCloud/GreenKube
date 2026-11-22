"""Primitive operations: the only edits patch strategies and render validation know about (H0b).

An action kind compiles its typed operations into primitives. A primitive names one
Kubernetes resource (``ResourceRef``) and, for field edits, a path inside it:

- ``/spec/replicas`` is a JSON-pointer-like path of mapping keys (``~1`` escapes ``/``,
  ``~0`` escapes ``~``);
- ``[name=api]`` selects the single list element whose ``name`` is ``api``, the way a
  strategic merge patch identifies list entries
  (``/spec/template/spec/containers/[name=api]/resources/requests/cpu``).

A selector never creates a list element: everything up to the last selector must exist in
the target. Mapping keys after it are created when missing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import Field, field_validator

from greenkube.gitops.contracts import ContractModel, ResourceTarget

#: A Kubernetes resource identity (apiVersion, kind, namespace, name).
ResourceRef = ResourceTarget


class PrimitiveApplyError(ValueError):
    """A primitive operation cannot be applied to its target manifest."""


class ListSelector(NamedTuple):
    """Select the unique list element whose ``key`` field equals ``value``."""

    key: str
    value: str


PathSegment = str | ListSelector


def parse_path(path: str) -> tuple[PathSegment, ...]:
    """Parse a primitive field path into mapping keys and list selectors."""
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise ValueError("a field path must start with '/' and name at least one field")
    segments: list[PathSegment] = []
    for raw in path[1:].split("/"):
        if not raw:
            raise ValueError("a field path must not contain empty segments")
        if raw.startswith("[") and raw.endswith("]"):
            key, separator, value = raw[1:-1].partition("=")
            if not separator or not key or not value:
                raise ValueError(f"list selector {raw!r} must have the form [key=value]")
            segments.append(ListSelector(_unescape(key), _unescape(value)))
        else:
            segments.append(_unescape(raw))
    return tuple(segments)


def format_path(segments: Iterable[PathSegment]) -> str:
    """Format path segments as a primitive field path."""
    parts: list[str] = []
    for segment in segments:
        if isinstance(segment, ListSelector):
            parts.append(f"[{_escape(segment.key)}={_escape(segment.value)}]")
        else:
            parts.append(_escape(segment))
    if not parts:
        raise ValueError("a field path must name at least one field")
    return "/" + "/".join(parts)


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


class _FieldOperation(ContractModel):
    ref: ResourceRef
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        parse_path(value)
        return value

    @property
    def segments(self) -> tuple[PathSegment, ...]:
        return parse_path(self.path)


class SetField(_FieldOperation):
    """Set one field of an existing resource."""

    op: Literal["set"] = "set"
    value: Any


class RemoveField(_FieldOperation):
    """Remove one field of an existing resource (a no-op when it is absent)."""

    op: Literal["remove"] = "remove"


class PlacementHint(ContractModel):
    """Where a strategy should write a new resource (next to ``near``, optionally in ``file_name``)."""

    near: ResourceRef | None = None
    file_name: str | None = None


class CreateResource(ContractModel):
    """Add a complete resource manifest."""

    op: Literal["create"] = "create"
    manifest: dict[str, Any]
    placement: PlacementHint = Field(default_factory=PlacementHint)

    @property
    def ref(self) -> ResourceRef:
        metadata = self.manifest.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        return ResourceRef(
            api_version=str(self.manifest.get("apiVersion") or ""),
            kind=str(self.manifest.get("kind") or ""),
            namespace=str(metadata.get("namespace") or ""),
            name=str(metadata.get("name") or ""),
        )


class DeleteResource(ContractModel):
    """Remove a complete resource."""

    op: Literal["delete"] = "delete"
    ref: ResourceRef


PrimitiveOp = Annotated[SetField | RemoveField | CreateResource | DeleteResource, Field(discriminator="op")]
FieldOp = SetField | RemoveField


def field_operations(primitives: Sequence[Any]) -> list[FieldOp]:
    """The field edits among ``primitives``, in order."""
    return [primitive for primitive in primitives if isinstance(primitive, (SetField, RemoveField))]


def apply_field_ops(
    manifest: MutableMapping[str, Any],
    operations: Sequence[FieldOp],
    *,
    new_mapping: Callable[[], MutableMapping[str, Any]] = dict,
) -> None:
    """Apply field edits in order to ``manifest`` (in place).

    ``new_mapping`` builds the mappings created for missing keys, so a round-trip YAML
    document receives ``CommentedMap`` nodes and keeps its formatting.
    """
    for operation in operations:
        segments = operation.segments
        last_selector = max(
            (index for index, segment in enumerate(segments) if isinstance(segment, ListSelector)), default=-1
        )
        parent: Any = manifest
        for index, segment in enumerate(segments[:-1]):
            parent = _descend(parent, segment, segments, index, create=index > last_selector, new_mapping=new_mapping)
            if parent is None:
                if isinstance(operation, RemoveField):
                    break
                raise PrimitiveApplyError(f"{_describe(segments, index)} is missing")
        else:
            leaf = segments[-1]
            if isinstance(leaf, ListSelector):
                raise PrimitiveApplyError("a field path must end with a field name, not a list selector")
            if not isinstance(parent, MutableMapping):
                raise PrimitiveApplyError(f"{_describe(segments, len(segments) - 2)} must be a mapping")
            if isinstance(operation, SetField):
                parent[leaf] = operation.value
            else:
                parent.pop(leaf, None)


def read_field(manifest: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Return ``(present, value)`` for ``path`` in ``manifest`` without changing it."""
    current: Any = manifest
    for segment in parse_path(path):
        if isinstance(segment, ListSelector):
            if not isinstance(current, list):
                return False, None
            matches = [item for item in current if isinstance(item, Mapping) and item.get(segment.key) == segment.value]
            if len(matches) != 1:
                return False, None
            current = matches[0]
        else:
            if not isinstance(current, Mapping) or segment not in current:
                return False, None
            current = current[segment]
    return True, current


def _descend(
    parent: Any,
    segment: PathSegment,
    segments: tuple[PathSegment, ...],
    index: int,
    *,
    create: bool,
    new_mapping: Callable[[], MutableMapping[str, Any]],
) -> Any:
    if isinstance(segment, ListSelector):
        if not isinstance(parent, list):
            raise PrimitiveApplyError(f"workload target must contain a {_list_name(segments, index)} list")
        list_name = _list_name(segments, index)
        matches: list[MutableMapping[str, Any]] = []
        for item in parent:
            if not isinstance(item, MutableMapping):
                raise PrimitiveApplyError(f"workload {list_name} must be mappings")
            key = item.get(segment.key)
            if not isinstance(key, str) or not key.strip():
                raise PrimitiveApplyError(f"workload {list_name} must have non-empty {segment.key}s")
            if key == segment.value:
                matches.append(item)
        if len(matches) != 1:
            raise PrimitiveApplyError(
                f"{_singular(list_name)} {segment.value!r} must exist exactly once in the workload"
            )
        return matches[0]
    if not isinstance(parent, MutableMapping):
        raise PrimitiveApplyError(f"{_describe(segments, index - 1)} must be a mapping")
    if segment not in parent:
        if not create:
            return None
        parent[segment] = new_mapping()
    child = parent[segment]
    next_segment = segments[index + 1]
    if not isinstance(next_segment, ListSelector) and not isinstance(child, MutableMapping):
        raise PrimitiveApplyError(f"{_describe(segments, index)} must be a mapping")
    return child


def _list_name(segments: tuple[PathSegment, ...], index: int) -> str:
    previous = segments[index - 1] if index > 0 else "items"
    return previous if isinstance(previous, str) else "items"


def _singular(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 1 else name


def _describe(segments: tuple[PathSegment, ...], index: int) -> str:
    """Human description of the node at ``segments[: index + 1]`` for error messages."""
    if index < 0:
        return "target resource"
    selector_index = max(
        (position for position, segment in enumerate(segments[: index + 1]) if isinstance(segment, ListSelector)),
        default=-1,
    )
    if selector_index >= 0:
        selector = segments[selector_index]
        assert isinstance(selector, ListSelector)
        owner = f"{_singular(_list_name(segments, selector_index))} {selector.value!r}"
        rest = [segment for segment in segments[selector_index + 1 : index + 1] if isinstance(segment, str)]
        return f"{owner} {'.'.join(rest)}" if rest else owner
    return ".".join(segment for segment in segments[: index + 1] if isinstance(segment, str))


def primitive_identity(ref: ResourceRef) -> tuple[str, str, str, str]:
    """``(apiVersion, kind, namespace, name)`` of a primitive's resource."""
    return ref.api_version, ref.kind, ref.namespace, ref.name


def primitive_ref(primitive: Any) -> ResourceRef:
    """The resource a primitive edits, creates or deletes."""
    return primitive.ref


def primitive_applied(primitive: Any, live: Mapping[str, Any] | None) -> bool:
    """Whether the live object (``None`` when absent) shows one primitive applied (H0c).

    ``SetField``: the field holds the value; ``RemoveField``: the field is absent;
    ``CreateResource``: the resource exists; ``DeleteResource``: it is absent.
    """
    if isinstance(primitive, DeleteResource):
        return live is None
    if isinstance(primitive, CreateResource):
        return live is not None
    if live is None:
        return False
    present, value = read_field(live, primitive.path)
    if isinstance(primitive, RemoveField):
        return not present
    return present and value == primitive.value


def invert_primitives(
    primitives: Sequence[Any],
    before: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
) -> list[PrimitiveOp]:
    """Primitives restoring ``before`` (the resources as they were), in reverse order (H0c).

    A created resource is deleted, a deleted one is created again from its manifest in
    ``before``, a set field gets its previous value back (or is removed when it was
    absent) and a removed field is set again. A resource or field missing from
    ``before`` cannot be restored: ``PrimitiveApplyError`` is raised rather than guessing.
    """
    inverse: list[PrimitiveOp] = []
    for primitive in reversed(list(primitives)):
        identity = primitive_identity(primitive.ref)
        if isinstance(primitive, CreateResource):
            inverse.append(DeleteResource(ref=primitive.ref))
            continue
        manifest = before.get(identity)
        if manifest is None:
            raise PrimitiveApplyError(f"{primitive.ref.kind} {primitive.ref.name!r} has no recorded state to restore")
        if isinstance(primitive, DeleteResource):
            inverse.append(CreateResource(manifest=_plain(manifest), placement=PlacementHint(near=primitive.ref)))
            continue
        present, value = read_field(manifest, primitive.path)
        if present:
            inverse.append(SetField(ref=primitive.ref, path=primitive.path, value=_plain(value)))
        elif isinstance(primitive, SetField):
            inverse.append(RemoveField(ref=primitive.ref, path=primitive.path))
    return inverse


def _plain(value: Any) -> Any:
    """A plain-Python deep copy (round-trip YAML nodes become dicts and lists)."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def strategic_merge_patch(primitives: Sequence[Any]) -> dict[str, Any]:
    """The field edits among ``primitives`` as one patch document (manual mode, H9).

    ``[name=api]`` selectors become list entries keyed by ``name`` (strategic merge), a
    removed field becomes ``null``. Create and delete primitives are not field edits and
    are left out.
    """
    patch: dict[str, Any] = {}
    for operation in field_operations(primitives):
        segments = operation.segments
        node: Any = patch
        for index, segment in enumerate(segments[:-1]):
            following = segments[index + 1]
            if isinstance(segment, ListSelector):
                entry = next((item for item in node if item.get(segment.key) == segment.value), None)
                if entry is None:
                    entry = {segment.key: segment.value}
                    node.append(entry)
                node = entry
            else:
                node = node.setdefault(segment, [] if isinstance(following, ListSelector) else {})
        leaf = segments[-1]
        if isinstance(leaf, ListSelector):
            raise PrimitiveApplyError("a field path must end with a field name, not a list selector")
        node[leaf] = operation.value if isinstance(operation, SetField) else None
    return patch
