"""Generic expected-delta render validation (H0c).

Every planner renders the source before and after its edit. The rendered resource set
must then differ exactly by the action's expected delta:

- the field edits the kind compiled (``SetField``/``RemoveField``), per resource;
- the resources it creates (``CreateResource``) and deletes (``DeleteResource``);
- nothing else: every other rendered resource is byte-for-byte unchanged.

Rendered ``checksum/*`` pod-template annotations are the only tolerated side effect,
since chart renderers recompute them from values. The kind's invariants (for example
request <= limit for container resources) are checked on the expected and the actual
result.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any

from greenkube.automation.actions.base import ActionKindError, ActionKindSpec
from greenkube.automation.actions.primitives import (
    CreateResource,
    DeleteResource,
    FieldOp,
    PrimitiveOp,
    apply_field_ops,
    field_operations,
)
from greenkube.automation.actions.workloads import pod_template_metadata_path
from greenkube.gitops.contracts import ResourceTarget, TypedOperation

ResourceIdentity = tuple[str, str, str, str]


class ExpectedDeltaError(ActionKindError):
    """The rendered change differs from the action's expected delta."""


def resource_identity(target: ResourceTarget) -> ResourceIdentity:
    return target.api_version, target.kind, target.namespace, target.name


def apply_action(
    manifest: MutableMapping[str, Any],
    spec: ActionKindSpec,
    target: ResourceTarget,
    operations: Sequence[TypedOperation],
    primitives: Sequence[PrimitiveOp] | None = None,
    *,
    new_mapping: Callable[[], MutableMapping[str, Any]] = dict,
) -> None:
    """Apply the kind's field edits of ``target`` to ``manifest`` in place and check its invariants.

    Raises ``ActionKindError`` (or ``PrimitiveApplyError``) when the manifest cannot take them.
    """
    compiled = spec.compile(target, operations) if primitives is None else primitives
    spec.validate_target(manifest, target)
    apply_field_ops(manifest, _field_ops_for(compiled, target), new_mapping=new_mapping)
    spec.check_invariants(manifest, target, operations)


def validate_expected_delta(
    before: Mapping[ResourceIdentity, Mapping[str, Any]],
    after: Mapping[ResourceIdentity, Mapping[str, Any]],
    *,
    spec: ActionKindSpec,
    target: ResourceTarget,
    target_identity: ResourceIdentity,
    operations: Sequence[TypedOperation],
) -> None:
    """Fail unless ``after`` equals ``before`` changed by exactly the action's expected delta.

    ``target_identity`` is the rendered identity of ``target`` (a controller may supply the
    namespace of a namespace-less rendered workload).
    """
    if target_identity not in before or target_identity not in after:
        raise ExpectedDeltaError("render validation did not preserve the exact target resource")
    primitives = spec.compile(target, operations)
    created = {
        resource_identity(primitive.ref): primitive.manifest
        for primitive in primitives
        if isinstance(primitive, CreateResource)
    }
    deleted = {resource_identity(primitive.ref) for primitive in primitives if isinstance(primitive, DeleteResource)}
    if set(after) != (set(before) - deleted) | set(created) or set(created) & set(before):
        raise ExpectedDeltaError("render validation found added or removed resources")

    expected_changes: dict[ResourceIdentity, list[FieldOp]] = {}
    for primitive in field_operations(primitives):
        identity = target_identity if primitive.ref == target else resource_identity(primitive.ref)
        expected_changes.setdefault(identity, []).append(primitive)
    expected_changes.setdefault(target_identity, [])

    for identity, manifest in before.items():
        if identity not in expected_changes and identity not in deleted and manifest != after[identity]:
            raise ExpectedDeltaError("render validation found an unrelated resource change")
    for identity, manifest in created.items():
        if after[identity] != manifest:
            raise ExpectedDeltaError(f"render validation found an unexpected {identity[1]} {identity[3]!r}")

    for identity, changes in expected_changes.items():
        if identity not in before or identity not in after:
            raise ExpectedDeltaError("render validation did not preserve a changed resource")
        expected = copy.deepcopy(before[identity])
        if not isinstance(expected, MutableMapping):
            raise ExpectedDeltaError("rendered target is not a mutable resource mapping")
        if identity == target_identity:
            spec.validate_target(expected, target)
        apply_field_ops(expected, changes)
        if identity == target_identity:
            spec.check_invariants(expected, target, operations)
        expected_comparison, expected_checksums = normalize_checksum_annotations(expected, identity[1])
        actual_comparison, actual_checksums = normalize_checksum_annotations(after[identity], identity[1])
        if expected_checksums != actual_checksums or expected_comparison != actual_comparison:
            raise ExpectedDeltaError(f"render validation found changes outside the requested {spec.description}")
    actual_target = after[target_identity]
    if not isinstance(actual_target, MutableMapping):
        raise ExpectedDeltaError("rendered target is not a mutable resource mapping")
    spec.check_invariants(actual_target, target, operations)


def normalize_checksum_annotations(manifest: Mapping[str, Any], kind: str) -> tuple[dict[str, Any], frozenset[str]]:
    """A copy of ``manifest`` whose pod-template ``checksum/*`` annotation values are masked."""
    comparison = copy.deepcopy(dict(manifest))
    metadata_path = pod_template_metadata_path(kind)
    if metadata_path is None:
        return comparison, frozenset()
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


def _field_ops_for(primitives: Sequence[PrimitiveOp], target: ResourceTarget) -> list[FieldOp]:
    return [primitive for primitive in field_operations(primitives) if primitive.ref == target]
