"""Bounded and defensive repository snapshot construction."""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import re
import stat
import struct
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Set
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO

from greenkube.gitops.snapshots.models import (
    RepositorySnapshot,
    SnapshotFile,
    SnapshotLimits,
    SnapshotMetadata,
)

_COMMIT_SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_CHUNK_SIZE = 64 * 1024
_ARCHIVE_ERRORS = (
    EOFError,
    OSError,
    ValueError,
    tarfile.TarError,
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    struct.error,
    NotImplementedError,
    RuntimeError,
)


class SnapshotError(ValueError):
    """Base class for invalid snapshot input or unsafe archive data."""


class SnapshotLimitError(SnapshotError):
    """The archive or extracted repository exceeded a configured limit."""


class SnapshotArchiveError(SnapshotError):
    """The archive is malformed, unsupported, or cannot be extracted."""


class UnsafeArchiveError(SnapshotArchiveError):
    """The archive contains an unsafe path or unsupported filesystem entry."""


class SnapshotIdentityError(SnapshotError):
    """The repository or immutable commit identity is invalid."""


class _ExtractionState:
    def __init__(self, workspace: Path, limits: SnapshotLimits) -> None:
        self.workspace = workspace
        self.limits = limits
        self.files: list[SnapshotFile] = []
        self.extracted_bytes = 0
        self._entries: set[str] = set()

    def add_directory(self, archive_path: str) -> None:
        relative_path = _safe_relative_path(archive_path, is_directory=True)
        if relative_path is None:
            return
        self._register_entry(relative_path)
        destination = self._destination(relative_path)
        try:
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise UnsafeArchiveError("archive paths conflict with an existing file or directory") from exc
        if not destination.is_dir():
            raise UnsafeArchiveError("archive paths conflict with an existing file or directory")

    def add_file(self, archive_path: str, source: IO[bytes], declared_size: int) -> None:
        relative_path = _safe_relative_path(archive_path, is_directory=False)
        if relative_path is None:
            raise UnsafeArchiveError("file path must identify a relative repository path")
        if declared_size < 0:
            raise SnapshotArchiveError("archive file has an invalid negative size")
        if declared_size > self.limits.max_file_bytes:
            raise SnapshotLimitError(f"file size exceeds the {self.limits.max_file_bytes}-byte file limit")
        if declared_size > self.limits.max_extracted_bytes - self.extracted_bytes:
            raise SnapshotLimitError(f"archive exceeds the {self.limits.max_extracted_bytes}-byte extracted byte limit")
        if len(self.files) >= self.limits.max_files:
            raise SnapshotLimitError(f"archive exceeds the {self.limits.max_files}-file limit")

        self._register_entry(relative_path)
        destination = self._destination(relative_path)
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            output_file = destination.open("xb")
        except (FileExistsError, IsADirectoryError, NotADirectoryError) as exc:
            raise UnsafeArchiveError("archive paths conflict with an existing file or directory") from exc
        except OSError as exc:
            raise SnapshotArchiveError("snapshot file could not be created") from exc

        digest = hashlib.sha256()
        written = 0
        try:
            with output_file as output:
                while True:
                    remaining_file = self.limits.max_file_bytes - written
                    remaining_total = self.limits.max_extracted_bytes - self.extracted_bytes - written
                    remaining_declared = declared_size - written
                    read_size = min(_CHUNK_SIZE, remaining_file + 1, remaining_total + 1, remaining_declared + 1)
                    chunk = source.read(read_size)
                    if not chunk:
                        break

                    written += len(chunk)
                    if written > declared_size:
                        raise SnapshotArchiveError("archive file content exceeds its declared size")
                    if written > self.limits.max_file_bytes:
                        raise SnapshotLimitError(f"file size exceeds the {self.limits.max_file_bytes}-byte file limit")
                    if self.extracted_bytes + written > self.limits.max_extracted_bytes:
                        raise SnapshotLimitError(
                            f"archive exceeds the {self.limits.max_extracted_bytes}-byte extracted byte limit"
                        )
                    output.write(chunk)
                    digest.update(chunk)
        except SnapshotError:
            raise
        except OSError as exc:
            raise SnapshotArchiveError("archive file data could not be read or written") from exc

        if written != declared_size:
            raise SnapshotArchiveError("archive file content is shorter than its declared size")

        self.extracted_bytes += written
        self.files.append(
            SnapshotFile(
                path=relative_path,
                size_bytes=written,
                digest=f"sha256:{digest.hexdigest()}",
            )
        )

    def ensure_new_file(self, relative_path: str) -> None:
        if relative_path in self._entries:
            raise UnsafeArchiveError("provider file collection contains duplicate paths")
        if len(self.files) >= self.limits.max_files:
            raise SnapshotLimitError(f"provider files exceed the {self.limits.max_files}-file limit")

    def _destination(self, relative_path: str) -> Path:
        return self.workspace.joinpath(*PurePosixPath(relative_path).parts)

    def _register_entry(self, relative_path: str) -> None:
        if relative_path in self._entries:
            raise UnsafeArchiveError("archive contains duplicate paths")
        if len(self._entries) >= self.limits.max_files:
            raise SnapshotLimitError(f"archive exceeds the {self.limits.max_files}-entry limit")
        self._entries.add(relative_path)


def _safe_relative_path(archive_path: str, *, is_directory: bool) -> str | None:
    if not archive_path or "\x00" in archive_path:
        raise UnsafeArchiveError("archive path is empty or contains a NUL byte")
    if "\\" in archive_path or ":" in archive_path:
        raise UnsafeArchiveError("archive path uses a platform-specific separator or drive")
    if PureWindowsPath(archive_path).drive:
        raise UnsafeArchiveError("archive path must not be absolute or drive-qualified")

    path = PurePosixPath(archive_path)
    if path.is_absolute():
        raise UnsafeArchiveError("archive path must be relative to the repository")
    if any(part == ".." for part in path.parts):
        raise UnsafeArchiveError("archive path must not traverse outside the repository")
    if not path.parts or path.as_posix() == ".":
        if is_directory:
            return None
        raise UnsafeArchiveError("archive path must identify a repository file")
    return path.as_posix()


def _provider_archive_path_parts(archive_path: str, *, is_directory: bool) -> tuple[str, ...]:
    _safe_relative_path(archive_path, is_directory=is_directory)
    path = archive_path[:-1] if is_directory and archive_path.endswith("/") else archive_path
    parts = tuple(path.split("/"))
    if not path or any(part in ("", ".", "..") for part in parts):
        raise UnsafeArchiveError("provider archive path is not canonical")
    return parts


def _provider_archive_root(archive_path: str, *, is_directory: bool) -> str:
    parts = _provider_archive_path_parts(archive_path, is_directory=is_directory)
    if len(parts) == 1 and is_directory:
        return parts[0]
    if len(parts) < 2:
        raise UnsafeArchiveError("provider archive is missing its repository root directory")
    return parts[0]


def _strip_provider_archive_root(archive_path: str, root: str, *, is_directory: bool) -> str | None:
    parts = _provider_archive_path_parts(archive_path, is_directory=is_directory)
    if parts[0] != root:
        raise UnsafeArchiveError("provider archive contains entries outside its repository root")
    if len(parts) == 1:
        if is_directory:
            return None
        raise UnsafeArchiveError("provider archive contains a file at its repository root marker")
    relative_path = "/".join(parts[1:])
    if is_directory and archive_path.endswith("/"):
        relative_path += "/"
    return relative_path


def _snapshot_identity(provider_id: str, repo_url: str, commit_sha: str) -> str:
    identity = json.dumps(
        {
            "version": 1,
            "provider_id": provider_id,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(identity).hexdigest()}"


def _tree_digest(files: tuple[SnapshotFile, ...]) -> str:
    tree = json.dumps(
        [[item.path, item.size_bytes, item.digest] for item in files],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(tree).hexdigest()}"


def _provider_file_entries(
    files: Mapping[str, str | bytes] | Iterable[tuple[str, str | bytes]],
) -> Iterable[object]:
    if isinstance(files, Mapping):
        return files.items()
    if isinstance(files, Iterable) and not isinstance(files, (str, bytes, bytearray)):
        return files
    raise SnapshotError("files must be a mapping or an iterable of path-content pairs")


def _unpack_provider_file_entry(entry: object) -> tuple[object, object]:
    if isinstance(entry, (str, bytes, bytearray, Mapping, Set)) or not isinstance(entry, Iterable):
        raise SnapshotError("each provider file must be a path-content pair")
    iterator = iter(entry)
    missing = object()
    path = next(iterator, missing)
    content = next(iterator, missing)
    extra = next(iterator, missing)
    if path is missing or content is missing or extra is not missing:
        raise SnapshotError("each provider file must be a path-content pair")
    return path, content


def _canonical_provider_path(path: object) -> str:
    if not isinstance(path, str) or path != path.strip():
        raise UnsafeArchiveError("provider file path must be a canonical repo-relative path")
    relative_path = _safe_relative_path(path, is_directory=False)
    if relative_path is None or relative_path != path:
        raise UnsafeArchiveError("provider file path must be canonical and repo-relative")
    return relative_path


def _provider_file_bytes(content: object, state: _ExtractionState) -> bytes:
    if not isinstance(content, (bytes, str)):
        raise SnapshotError("provider file content must be bytes or text")

    minimum_size = len(content)
    if minimum_size > state.limits.max_file_bytes:
        raise SnapshotLimitError(f"file size exceeds the {state.limits.max_file_bytes}-byte file limit")
    if minimum_size > state.limits.max_extracted_bytes - state.extracted_bytes:
        raise SnapshotLimitError(f"snapshot exceeds the {state.limits.max_extracted_bytes}-byte extracted byte limit")

    if isinstance(content, bytes):
        return content
    try:
        return content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SnapshotError("provider file text must be encodable as UTF-8") from exc


def _extract_zip(
    archive_data: bytes,
    state: _ExtractionState,
    *,
    strip_top_level_directory: bool,
) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        infos = archive.infolist()
        root = None
        root_seen = False
        if strip_top_level_directory:
            if not infos:
                raise UnsafeArchiveError("provider archive is missing its repository root directory")
            root = _provider_archive_root(infos[0].filename, is_directory=infos[0].is_dir())

        for info in infos:
            original_name = getattr(info, "orig_filename", info.filename)
            if original_name != info.filename or "\x00" in original_name:
                raise UnsafeArchiveError("archive path contains an invalid NUL byte")
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise UnsafeArchiveError("symbolic links are not supported in repository archives")
            if file_type in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
                raise UnsafeArchiveError("device and special files are not supported in repository archives")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeArchiveError("unsupported filesystem entry in repository archive")
            if info.flag_bits & 0x1:
                raise SnapshotArchiveError("encrypted ZIP entries are not supported")
            if info.file_size < 0:
                raise SnapshotArchiveError("ZIP entry has an invalid negative size")
            if info.is_dir():
                if file_type == stat.S_IFREG or info.file_size != 0:
                    raise UnsafeArchiveError("ZIP directory entry has an incompatible type or size")
            elif file_type == stat.S_IFDIR:
                raise UnsafeArchiveError("ZIP directory entry is missing its directory marker")

            archive_path = info.filename
            if root is not None:
                archive_path = _strip_provider_archive_root(
                    info.filename,
                    root,
                    is_directory=info.is_dir(),
                )
                if archive_path is None:
                    if root_seen:
                        raise UnsafeArchiveError("provider archive contains a duplicate repository root directory")
                    root_seen = True
                    continue
            if info.is_dir():
                state.add_directory(archive_path)
                continue
            with archive.open(info, mode="r") as source:
                state.add_file(archive_path, source, info.file_size)


def _extract_tar(
    archive_data: bytes,
    state: _ExtractionState,
    *,
    strip_top_level_directory: bool,
) -> None:
    root = None
    root_seen = False
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r|*") as archive:
        for member in archive:
            if member.issym():
                raise UnsafeArchiveError("symbolic links are not supported in repository archives")
            if member.islnk():
                raise UnsafeArchiveError("hard links are not supported in repository archives")
            if member.isdev() or member.isfifo():
                raise UnsafeArchiveError("device and special files are not supported in repository archives")
            if not member.isdir() and not member.isreg():
                raise UnsafeArchiveError("unsupported filesystem entry in repository archive")
            archive_path = member.name
            if strip_top_level_directory:
                if root is None:
                    root = _provider_archive_root(member.name, is_directory=member.isdir())
                archive_path = _strip_provider_archive_root(
                    member.name,
                    root,
                    is_directory=member.isdir(),
                )
                if archive_path is None:
                    if root_seen:
                        raise UnsafeArchiveError("provider archive contains a duplicate repository root directory")
                    root_seen = True
                    continue
            if member.isdir():
                state.add_directory(archive_path)
                continue
            if member.name.endswith("/"):
                raise UnsafeArchiveError("regular file path must not end with a directory separator")
            if member.sparse:
                raise UnsafeArchiveError("sparse files are not supported in repository archives")
            source = archive.extractfile(member)
            if source is None:
                raise SnapshotArchiveError("tar file entry has no readable content")
            with source:
                state.add_file(archive_path, source, member.size)
    if strip_top_level_directory and root is None:
        raise UnsafeArchiveError("provider archive is missing its repository root directory")


def extract_archive(
    archive_data: bytes,
    *,
    provider_id: str,
    repo_url: str,
    commit_sha: str,
    limits: SnapshotLimits | None = None,
    workspace_parent: str | Path | None = None,
    strip_top_level_directory: bool = False,
) -> RepositorySnapshot:
    """Extract a tar or ZIP archive into an owned, isolated temporary workspace.

    ``strip_top_level_directory`` handles provider-generated archives whose entries are all rooted
    under one repository-name directory. Every entry must remain under that same root.
    """
    if not isinstance(archive_data, bytes):
        raise SnapshotArchiveError("archive data must be supplied as bytes")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise SnapshotIdentityError("provider_id must not be blank")
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise SnapshotIdentityError("repo_url must not be blank")
    if not isinstance(commit_sha, str) or not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise SnapshotIdentityError("commit_sha must be a full 40- or 64-character hexadecimal SHA")
    commit_sha = commit_sha.lower()
    effective_limits = limits if limits is not None else SnapshotLimits()
    if not isinstance(effective_limits, SnapshotLimits):
        raise SnapshotIdentityError("limits must be a SnapshotLimits instance")
    if type(strip_top_level_directory) is not bool:
        raise SnapshotIdentityError("strip_top_level_directory must be a boolean")
    if len(archive_data) > effective_limits.max_compressed_bytes:
        raise SnapshotLimitError(
            f"archive exceeds the {effective_limits.max_compressed_bytes}-byte compressed byte limit"
        )

    parent_path: Path | None = None
    if workspace_parent is not None:
        parent_path = Path(workspace_parent)
        if not parent_path.is_dir():
            raise SnapshotError("workspace_parent must be an existing directory")

    try:
        owner = tempfile.TemporaryDirectory(
            prefix="greenkube-snapshot-",
            dir=str(parent_path) if parent_path is not None else None,
        )
    except OSError as exc:
        raise SnapshotError("could not create an isolated snapshot workspace") from exc

    workspace = Path(owner.name)
    state = _ExtractionState(workspace, effective_limits)
    try:
        try:
            if zipfile.is_zipfile(io.BytesIO(archive_data)):
                _extract_zip(
                    archive_data,
                    state,
                    strip_top_level_directory=strip_top_level_directory,
                )
            else:
                _extract_tar(
                    archive_data,
                    state,
                    strip_top_level_directory=strip_top_level_directory,
                )
        except SnapshotError:
            raise
        except _ARCHIVE_ERRORS as exc:
            raise SnapshotArchiveError(f"archive is malformed or unsupported ({type(exc).__name__})") from exc

        files = tuple(sorted(state.files, key=lambda item: item.path))
        archive_digest = f"sha256:{hashlib.sha256(archive_data).hexdigest()}"
        metadata = SnapshotMetadata(
            provider_id=provider_id,
            repo_url=repo_url,
            commit_sha=commit_sha,
            snapshot_id=_snapshot_identity(provider_id, repo_url, commit_sha),
            archive_digest=archive_digest,
            tree_digest=_tree_digest(files),
            compressed_bytes=len(archive_data),
            extracted_bytes=state.extracted_bytes,
            file_count=len(files),
            source_kind="archive",
            source_digest=archive_digest,
        )
        return RepositorySnapshot(metadata, workspace, files, owner)
    except BaseException:
        owner.cleanup()
        raise


def snapshot_from_files(
    files: Mapping[str, str | bytes] | Iterable[tuple[str, str | bytes]],
    *,
    provider_id: str,
    repo_url: str,
    commit_sha: str,
    limits: SnapshotLimits | None = None,
    workspace_parent: str | Path | None = None,
) -> RepositorySnapshot:
    """Create an owned snapshot from provider-returned repository file contents."""
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise SnapshotIdentityError("provider_id must not be blank")
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise SnapshotIdentityError("repo_url must not be blank")
    if not isinstance(commit_sha, str) or not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise SnapshotIdentityError("commit_sha must be a full 40- or 64-character hexadecimal SHA")
    commit_sha = commit_sha.lower()
    effective_limits = limits if limits is not None else SnapshotLimits()
    if not isinstance(effective_limits, SnapshotLimits):
        raise SnapshotIdentityError("limits must be a SnapshotLimits instance")

    file_entries = _provider_file_entries(files)
    parent_path: Path | None = None
    if workspace_parent is not None:
        parent_path = Path(workspace_parent)
        if not parent_path.is_dir():
            raise SnapshotError("workspace_parent must be an existing directory")

    try:
        owner = tempfile.TemporaryDirectory(
            prefix="greenkube-snapshot-",
            dir=str(parent_path) if parent_path is not None else None,
        )
    except OSError as exc:
        raise SnapshotError("could not create an isolated snapshot workspace") from exc

    workspace = Path(owner.name)
    state = _ExtractionState(workspace, effective_limits)
    try:
        for entry in file_entries:
            path, content = _unpack_provider_file_entry(entry)
            relative_path = _canonical_provider_path(path)
            state.ensure_new_file(relative_path)
            content_bytes = _provider_file_bytes(content, state)
            state.add_file(relative_path, io.BytesIO(content_bytes), len(content_bytes))

        snapshot_files = tuple(sorted(state.files, key=lambda item: item.path))
        tree_digest = _tree_digest(snapshot_files)
        metadata = SnapshotMetadata(
            provider_id=provider_id,
            repo_url=repo_url,
            commit_sha=commit_sha,
            snapshot_id=_snapshot_identity(provider_id, repo_url, commit_sha),
            archive_digest=None,
            tree_digest=tree_digest,
            compressed_bytes=0,
            extracted_bytes=state.extracted_bytes,
            file_count=len(snapshot_files),
            source_kind="provider_files",
            source_digest=tree_digest,
        )
        return RepositorySnapshot(metadata, workspace, snapshot_files, owner)
    except BaseException:
        owner.cleanup()
        raise
