"""Decide whether a later revision of a planned file still carries the planned values.

A merged plan is only confirmed on a *descendant* commit (roadmap D1, BUG-43)
when the commits that landed on top of the merge did not revert or overwrite
what the plan changed. This module compares three versions of one YAML/JSON
file structurally: the plan's ``original`` (``None`` for a created file), the
``planned`` content and the ``observed`` content at the controller's revision.

Every leaf the plan set must hold its planned value in ``observed``, and every
leaf the plan removed must still be absent. Edits elsewhere in the file (an
image tag bump, another container, a new document) are allowed. Kubernetes
lists of named items (containers, env) are matched by ``name`` and documents by
resource identity, so reordering them does not hide the planned values.

The comparison is conservative: content that does not parse, or exceeds the
structural bounds, never counts as carrying the planned values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeAlias

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

#: Upper bound on flattened leaves per file (guards against alias expansion).
MAX_PLANNED_VALUE_LEAVES = 50_000
#: Upper bound on nesting depth per file.
MAX_PLANNED_VALUE_DEPTH = 64

_MISSING = object()

_PathKey: TypeAlias = tuple[str, ...]
_Leaf: TypeAlias = tuple[str, str | None]
_Leaves: TypeAlias = dict[tuple[_PathKey, ...], _Leaf]


class _StructureLimitError(ValueError):
    """The document is too large or too deep to compare safely."""


def planned_values_present(original: str | None, planned: str, observed: str) -> bool:
    """Return whether ``observed`` still carries every change from ``original`` to ``planned``."""
    try:
        before = _flatten_documents(original) if original is not None else {}
        after = _flatten_documents(planned)
        current = _flatten_documents(observed)
    except (YAMLError, _StructureLimitError, RecursionError, TypeError, ValueError):
        return False
    for path, value in after.items():
        if before.get(path, _MISSING) != value and current.get(path, _MISSING) != value:
            return False
    return not any(path in current for path in before.keys() - after.keys())


def _flatten_documents(content: str) -> _Leaves:
    if not isinstance(content, str):
        raise TypeError("file content must be text")
    documents = [document for document in YAML(typ="safe", pure=True).load_all(content) if document is not None]
    leaves: _Leaves = {}
    for key, document in zip(_document_keys(documents), documents, strict=True):
        _flatten(document, (key,), leaves)
    return leaves


def _document_keys(documents: list[Any]) -> list[_PathKey]:
    identities: list[_PathKey] = []
    for document in documents:
        identity = _resource_identity(document)
        if identity is None:
            break
        identities.append(identity)
    else:
        if len(set(identities)) == len(identities):
            return identities
    return [("document", str(index)) for index in range(len(documents))]


def _resource_identity(document: Any) -> _PathKey | None:
    if not isinstance(document, Mapping):
        return None
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    kind, name = document.get("kind"), metadata.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        return None
    api_version, namespace = document.get("apiVersion"), metadata.get("namespace")
    return (
        "resource",
        api_version if isinstance(api_version, str) else "",
        kind,
        namespace if isinstance(namespace, str) else "",
        name,
    )


def _flatten(node: Any, path: tuple[_PathKey, ...], leaves: _Leaves) -> None:
    if len(path) > MAX_PLANNED_VALUE_DEPTH:
        raise _StructureLimitError("document is nested too deeply")
    if isinstance(node, Mapping) and node:
        for key, value in node.items():
            _flatten(value, (*path, ("key", _scalar_text(key))), leaves)
        return
    if isinstance(node, list) and node:
        for key, item in zip(_list_item_keys(node), node, strict=True):
            _flatten(item, (*path, key), leaves)
        return
    if len(leaves) >= MAX_PLANNED_VALUE_LEAVES:
        raise _StructureLimitError("document has too many values")
    leaves[path] = _leaf(node)


def _list_item_keys(items: list[Any]) -> list[_PathKey]:
    names = [item.get("name") if isinstance(item, Mapping) else None for item in items]
    if all(isinstance(name, str) for name in names) and len(set(names)) == len(names):
        return [("name", str(name)) for name in names]
    return [("index", str(index)) for index in range(len(items))]


def _leaf(value: Any) -> _Leaf:
    if isinstance(value, Mapping):
        return ("map", None)
    if isinstance(value, list):
        return ("list", None)
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", str(value).lower())
    if isinstance(value, (str, int, float)):
        # ``cpu: 1`` and ``cpu: "1"`` carry the same Kubernetes quantity.
        return ("scalar", str(value))
    return ("other", repr(value))


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)
