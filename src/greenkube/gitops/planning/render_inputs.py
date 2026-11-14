"""Render inputs of a plan: the repository files a render read (D7, delivery safety).

A plan is rendered on a base revision. When the branch head moves before the plan is
applied, the edit is only still valid if nothing the render read changed: not just the
files the plan edits, but the Kustomize bases and patches, the Helm values files and
chart files, and the raw manifests of the rendered directory. Planners record those
files with their Git blob IDs (``ValidationResult.render_inputs``) and the directories
where an added file changes the render (``ValidationResult.render_scopes``); the
approval and the worker accept a moved head only when none of them changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any

from greenkube.gitops.contracts import FileEdit, ValidationResult
from greenkube.gitops.sha import GIT_OBJECT_SHA

_READS: ContextVar[set[str] | None] = ContextVar("greenkube_render_input_reads", default=None)


def git_blob_sha(content: bytes | str, base_revision: str) -> str:
    """The Git blob ID of ``content`` in a repository whose commits look like ``base_revision``."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    algorithm = hashlib.sha256 if len(base_revision) == 64 else hashlib.sha1
    return algorithm(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


@contextmanager
def recording_reads() -> Iterator[set[str]]:
    """Collect the snapshot paths read through :func:`note_read` inside the block."""
    reads: set[str] = set()
    token = _READS.set(reads)
    try:
        yield reads
    finally:
        _READS.reset(token)


def note_read(path: str) -> None:
    """Record that the active render read ``path`` (no-op outside :func:`recording_reads`)."""
    reads = _READS.get()
    if reads is not None:
        reads.add(path)


def workspace_blob_shas(root: Path, paths: Iterable[str], base_revision: str) -> dict[str, str]:
    """Git blob IDs of snapshot files read from the snapshot workspace ``root``."""
    shas: dict[str, str] = {}
    for path in sorted(set(paths)):
        shas[path] = git_blob_sha(root.joinpath(*PurePosixPath(path).parts).read_bytes(), base_revision)
    return shas


def contents_blob_shas(
    contents: Mapping[str, str | bytes], base_revision: str, known: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Git blob IDs of in-memory snapshot contents (``known`` IDs are used as recorded)."""
    known = known or {}
    shas: dict[str, str] = {}
    for path, content in contents.items():
        recorded = known.get(path)
        valid = isinstance(recorded, str) and GIT_OBJECT_SHA.fullmatch(recorded) is not None
        shas[path] = recorded if valid and recorded is not None else git_blob_sha(content, base_revision)
    return shas


def files_in_scope(files: Iterable[str], scope: str) -> list[str]:
    """The snapshot paths inside the directory ``scope`` (``.`` is the whole repository)."""
    return sorted(path for path in files if in_scope(path, scope))


def in_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(f"{scope}/")


def changed_render_inputs(validation: ValidationResult, changed_paths: Iterable[str]) -> list[str]:
    """The changed paths that a render read or that land in one of its render scopes."""
    inputs = validation.render_inputs
    scopes = validation.render_scopes
    return sorted(path for path in set(changed_paths) if path in inputs or any(in_scope(path, s) for s in scopes))


def records_render_inputs(validation: ValidationResult) -> bool:
    """Whether the plan was rendered with render-input recording (plans before it were not)."""
    return bool(validation.render_inputs or validation.render_scopes)


def inverse_render_inputs(original: ValidationResult, files: Iterable[FileEdit]) -> dict[str, Any]:
    """Render inputs of a rollback plan, derived from its original plan.

    The inverse is rendered on a later revision from the same overlay, chart or directory:
    the files it edits are inputs with their blob IDs at that revision, and every other
    input of the original render (whose blob ID at that revision is not known here) is
    watched as a scope, so any change to it after the rollback was planned stales it. A
    rollback of a plan without recorded inputs records none (stale on a moved head).
    """
    if not records_render_inputs(original):
        return {}
    inputs = {
        edit.path: edit.original_sha
        for edit in files
        if isinstance(edit.original_sha, str) and GIT_OBJECT_SHA.fullmatch(edit.original_sha)
    }
    edited = {edit.path for edit in files}
    scopes = {*original.render_scopes, *(path for path in original.render_inputs if path not in inputs)}
    scopes.update(path for path in edited if path not in inputs)
    return {"render_inputs": inputs, "render_scopes": sorted(scopes)}
