"""Typed metadata and lifetime management for repository snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Self


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    """Hard resource limits applied to untrusted repository snapshot inputs.

    ``max_compressed_bytes`` applies only to archives. ``max_files`` also caps explicit archive
    directory entries to bound metadata and inode use.
    """

    max_compressed_bytes: int = 100 * 1024 * 1024
    max_extracted_bytes: int = 500 * 1024 * 1024
    max_files: int = 20_000
    max_file_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("max_compressed_bytes", self.max_compressed_bytes),
            ("max_extracted_bytes", self.max_extracted_bytes),
            ("max_files", self.max_files),
            ("max_file_bytes", self.max_file_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """Digest and size metadata for one extracted repository file."""

    path: str
    size_bytes: int
    digest: str


@dataclass(frozen=True, slots=True)
class SnapshotMetadata:
    """Deterministic, persistence-safe metadata for a repository revision.

    ``archive_digest`` is unset for provider-file snapshots. ``source_digest`` is the archive
    digest or, for provider files, the deterministic digest of their sorted file tree.
    """

    provider_id: str
    repo_url: str
    commit_sha: str
    snapshot_id: str
    archive_digest: str | None
    tree_digest: str
    compressed_bytes: int
    extracted_bytes: int
    file_count: int
    source_kind: str = "archive"
    source_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """A snapshot with an isolated workspace that is explicitly disposable."""

    metadata: SnapshotMetadata
    workspace: Path
    files: tuple[SnapshotFile, ...]
    _workspace_owner: TemporaryDirectory[str] = field(repr=False, compare=False)

    def close(self) -> None:
        """Delete only the temporary directory owned by this snapshot."""
        self._workspace_owner.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
