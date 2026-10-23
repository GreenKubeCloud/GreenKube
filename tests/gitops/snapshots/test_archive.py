import hashlib
import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from greenkube.gitops.snapshots import (
    RepositorySnapshot,
    SnapshotArchiveError,
    SnapshotLimitError,
    SnapshotLimits,
    UnsafeArchiveError,
    extract_archive,
)


def _zip_archive(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(path, content)
    return buffer.getvalue()


def _tar_archive(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in entries:
            member = tarfile.TarInfo(path)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _extract(
    archive: bytes,
    *,
    limits: SnapshotLimits | None = None,
    workspace_parent: Path | None = None,
    commit_sha: str = "a" * 40,
    strip_top_level_directory: bool = False,
) -> RepositorySnapshot:
    return extract_archive(
        archive,
        provider_id="github-main",
        repo_url="https://github.com/acme/config.git",
        commit_sha=commit_sha,
        limits=limits,
        workspace_parent=workspace_parent,
        strip_top_level_directory=strip_top_level_directory,
    )


def test_extracts_zip_files_and_returns_typed_metadata_and_content_digests(tmp_path):
    archive = _zip_archive([("apps/api.yaml", b"kind: Deployment\n"), ("README.md", b"config")])

    with _extract(archive, workspace_parent=tmp_path) as snapshot:
        assert isinstance(snapshot, RepositorySnapshot)
        assert snapshot.workspace.is_dir()
        assert snapshot.metadata.compressed_bytes == len(archive)
        assert snapshot.metadata.extracted_bytes == len(b"kind: Deployment\nconfig")
        assert snapshot.metadata.file_count == 2
        assert snapshot.metadata.archive_digest == f"sha256:{hashlib.sha256(archive).hexdigest()}"
        assert snapshot.metadata.source_kind == "archive"
        assert snapshot.metadata.source_digest == snapshot.metadata.archive_digest
        assert [(item.path, item.size_bytes) for item in snapshot.files] == [
            ("README.md", 6),
            ("apps/api.yaml", 17),
        ]
        assert snapshot.files[0].digest == f"sha256:{hashlib.sha256(b'config').hexdigest()}"
        assert (snapshot.workspace / "apps/api.yaml").read_bytes() == b"kind: Deployment\n"


def test_extracts_gzip_tar_archives(tmp_path):
    archive = _tar_archive([("repo/config.json", b'{"kind":"ConfigMap"}')])

    with _extract(archive, workspace_parent=tmp_path) as snapshot:
        assert (snapshot.workspace / "repo/config.json").read_bytes() == b'{"kind":"ConfigMap"}'
        assert snapshot.metadata.file_count == 1


@pytest.mark.parametrize("archive_builder", [_zip_archive, _tar_archive])
def test_strips_provider_archive_root_directory_without_changing_repository_paths(tmp_path, archive_builder):
    archive = archive_builder(
        [
            ("manifests-a1b2c3d/apps/api.yaml", b"kind: Deployment\n"),
            ("manifests-a1b2c3d/README.md", b"config"),
        ]
    )

    with _extract(archive, workspace_parent=tmp_path, strip_top_level_directory=True) as snapshot:
        assert {item.path for item in snapshot.files} == {"apps/api.yaml", "README.md"}
        assert (snapshot.workspace / "apps/api.yaml").read_bytes() == b"kind: Deployment\n"


def test_provider_archive_root_stripping_rejects_multiple_repository_roots(tmp_path):
    archive = _zip_archive([("first/config.yaml", b"one"), ("second/config.yaml", b"two")])

    with pytest.raises(UnsafeArchiveError, match="outside its repository root"):
        _extract(archive, workspace_parent=tmp_path, strip_top_level_directory=True)

    assert list(tmp_path.iterdir()) == []


def test_provider_archive_root_stripping_rejects_rootless_archives(tmp_path):
    archive = _zip_archive([("README.md", b"config")])

    with pytest.raises(UnsafeArchiveError, match="missing its repository root"):
        _extract(archive, workspace_parent=tmp_path, strip_top_level_directory=True)

    assert list(tmp_path.iterdir()) == []


def test_provider_archive_root_stripping_rejects_duplicate_root_entries(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _zip_archive([("repository/", b""), ("repository/", b"")])

    with pytest.raises(UnsafeArchiveError, match="duplicate repository root"):
        _extract(archive, workspace_parent=tmp_path, strip_top_level_directory=True)

    assert list(tmp_path.iterdir()) == []


def test_rejects_archive_exceeding_compressed_byte_limit(tmp_path):
    archive = _zip_archive([("config.yaml", b"small")])

    with pytest.raises(SnapshotLimitError, match="compressed"):
        _extract(
            archive,
            limits=SnapshotLimits(max_compressed_bytes=len(archive) - 1),
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_rejects_archive_exceeding_total_extracted_byte_limit(tmp_path):
    archive = _zip_archive([("config.yaml", b"x" * 32)])

    with pytest.raises(SnapshotLimitError, match="extracted"):
        _extract(
            archive,
            limits=SnapshotLimits(max_extracted_bytes=16, max_file_bytes=64),
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_rejects_archive_exceeding_file_count_limit(tmp_path):
    archive = _zip_archive([("one.yaml", b"1"), ("two.yaml", b"2")])

    with pytest.raises(SnapshotLimitError, match="file"):
        _extract(archive, limits=SnapshotLimits(max_files=1), workspace_parent=tmp_path)


def test_directory_entries_cannot_bypass_the_file_count_limit(tmp_path):
    archive = _zip_archive([("one/", b""), ("two/", b"")])

    with pytest.raises(SnapshotLimitError, match="entry"):
        _extract(archive, limits=SnapshotLimits(max_files=1), workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_file_exceeding_per_file_byte_limit(tmp_path):
    archive = _zip_archive([("large.yaml", b"x" * 17)])

    with pytest.raises(SnapshotLimitError, match="file"):
        _extract(archive, limits=SnapshotLimits(max_file_bytes=16), workspace_parent=tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.yaml",
        "/absolute.yaml",
        "C:/outside.yaml",
        r"nested\..\outside.yaml",
    ],
)
@pytest.mark.parametrize("archive_builder", [_zip_archive, _tar_archive])
def test_rejects_traversal_and_absolute_paths(tmp_path, path, archive_builder):
    archive = archive_builder([(path, b"unsafe")])

    with pytest.raises(UnsafeArchiveError, match="path"):
        _extract(archive, workspace_parent=tmp_path)

    assert not (tmp_path / "escape.yaml").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("typeflag", "linkname", "message"),
    [
        (tarfile.SYMTYPE, "../outside", "symbolic link"),
        (tarfile.LNKTYPE, "file.yaml", "hard link"),
        (tarfile.CHRTYPE, "", "device"),
    ],
)
def test_rejects_tar_symlink_hardlink_and_device_entries(tmp_path, typeflag, linkname, message):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("unsafe")
        member.type = typeflag
        member.linkname = linkname
        archive.addfile(member)

    with pytest.raises(UnsafeArchiveError, match=message):
        _extract(buffer.getvalue(), workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_zip_symlink_entries(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../outside")

    with pytest.raises(UnsafeArchiveError, match="symbolic link"):
        _extract(buffer.getvalue(), workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_malformed_archives_with_an_explicit_error(tmp_path):
    with pytest.raises(SnapshotArchiveError, match="archive"):
        _extract(b"not a supported archive", workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_snapshot_identity_is_commit_addressed_and_tree_digest_ignores_archive_order(tmp_path):
    first_archive = _zip_archive([("a.yaml", b"a"), ("b.yaml", b"b")])
    reordered_archive = _zip_archive([("b.yaml", b"b"), ("a.yaml", b"a")])

    with _extract(first_archive, workspace_parent=tmp_path) as first:
        with _extract(reordered_archive, workspace_parent=tmp_path) as reordered:
            assert first.metadata.snapshot_id == reordered.metadata.snapshot_id
            assert first.metadata.tree_digest == reordered.metadata.tree_digest
            assert first.metadata.archive_digest != reordered.metadata.archive_digest
            assert first.metadata.commit_sha == "a" * 40

        with _extract(first_archive, workspace_parent=tmp_path, commit_sha="b" * 40) as other_commit:
            assert first.metadata.snapshot_id != other_commit.metadata.snapshot_id


def test_close_removes_only_the_snapshot_owned_workspace(tmp_path):
    parent = tmp_path / "workspace-parent"
    parent.mkdir()
    unrelated = parent / "keep.txt"
    unrelated.write_text("preserve", encoding="utf-8")

    snapshot = _extract(_zip_archive([("config.yaml", b"safe")]), workspace_parent=parent)
    workspace = snapshot.workspace

    assert workspace.parent == parent
    assert workspace.is_dir()
    snapshot.close()
    snapshot.close()

    assert not workspace.exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_failure_cleans_up_only_the_partial_snapshot_workspace(tmp_path):
    parent = tmp_path / "workspace-parent"
    parent.mkdir()
    archive = _tar_archive([("safe.yaml", b"safe"), ("../escape.yaml", b"unsafe")])

    with pytest.raises(UnsafeArchiveError):
        _extract(archive, workspace_parent=parent)

    assert parent.is_dir()
    assert list(parent.iterdir()) == []
