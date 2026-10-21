import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from greenkube.gitops.snapshots import (
    RepositorySnapshot,
    SnapshotError,
    SnapshotIdentityError,
    SnapshotLimitError,
    SnapshotLimits,
    UnsafeArchiveError,
    snapshot_from_files,
)

PROVIDER_ID = "github-main"
REPO_URL = "https://github.com/acme/config.git"
COMMIT_SHA = "a" * 40


def _snapshot(
    files: dict[str, str | bytes] | Iterable[tuple[str, str | bytes]],
    *,
    workspace_parent: Path | None = None,
    limits: SnapshotLimits | None = None,
    commit_sha: str = COMMIT_SHA,
) -> RepositorySnapshot:
    return snapshot_from_files(
        files,
        provider_id=PROVIDER_ID,
        repo_url=REPO_URL,
        commit_sha=commit_sha,
        limits=limits,
        workspace_parent=workspace_parent,
    )


def test_snapshots_provider_files_into_owned_workspace_and_records_digests(tmp_path):
    files = {
        "apps/api.yaml": "kind: Deployment\n",
        "README.md": b"config",
    }

    with _snapshot(files, workspace_parent=tmp_path) as snapshot:
        assert isinstance(snapshot, RepositorySnapshot)
        assert snapshot.workspace.parent == tmp_path
        assert snapshot.workspace.is_dir()
        assert [(item.path, item.size_bytes) for item in snapshot.files] == [
            ("README.md", 6),
            ("apps/api.yaml", 17),
        ]
        assert (snapshot.workspace / "README.md").read_bytes() == b"config"
        assert (snapshot.workspace / "apps/api.yaml").read_bytes() == b"kind: Deployment\n"
        assert snapshot.metadata.provider_id == PROVIDER_ID
        assert snapshot.metadata.repo_url == REPO_URL
        assert snapshot.metadata.commit_sha == COMMIT_SHA
        assert snapshot.metadata.source_kind == "provider_files"
        assert snapshot.metadata.archive_digest is None
        assert snapshot.metadata.source_digest == snapshot.metadata.tree_digest
        assert snapshot.metadata.compressed_bytes == 0
        assert snapshot.metadata.extracted_bytes == 23
        assert snapshot.metadata.file_count == 2

        expected_file_metadata = [
            [
                "README.md",
                6,
                f"sha256:{hashlib.sha256(b'config').hexdigest()}",
            ],
            [
                "apps/api.yaml",
                17,
                f"sha256:{hashlib.sha256(b'kind: Deployment\n').hexdigest()}",
            ],
        ]
        expected_tree = json.dumps(
            expected_file_metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert snapshot.metadata.tree_digest == f"sha256:{hashlib.sha256(expected_tree).hexdigest()}"


def test_file_snapshots_are_deterministic_across_input_order_and_text_encoding(tmp_path):
    entries = [("z.yaml", b"z"), ("a.yaml", "é")]

    with _snapshot(entries, workspace_parent=tmp_path) as first:
        with _snapshot(iter(reversed(entries)), workspace_parent=tmp_path) as second:
            assert first.files == second.files
            assert first.metadata.snapshot_id == second.metadata.snapshot_id
            assert first.metadata.tree_digest == second.metadata.tree_digest
            assert first.metadata.source_digest == second.metadata.source_digest
            assert first.metadata.extracted_bytes == 3
            assert first.metadata.commit_sha == COMMIT_SHA


def test_normalizes_commit_sha_case_for_file_snapshots(tmp_path):
    with _snapshot({"config.yaml": b"safe"}, workspace_parent=tmp_path, commit_sha="A" * 40) as snapshot:
        assert snapshot.metadata.commit_sha == "a" * 40


def test_supports_an_empty_provider_file_collection(tmp_path):
    with _snapshot({}, workspace_parent=tmp_path) as snapshot:
        assert snapshot.files == ()
        assert snapshot.metadata.file_count == 0
        assert snapshot.metadata.extracted_bytes == 0
        assert snapshot.workspace.is_dir()


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "./config.yaml",
        "a//b.yaml",
        "a/./b.yaml",
        "directory/",
        "../escape.yaml",
        "a/../b.yaml",
        "/absolute.yaml",
        "C:/outside.yaml",
        r"nested\config.yaml",
        " config.yaml",
        "config.yaml ",
        "a\x00b",
        Path("config.yaml"),
    ],
)
def test_rejects_noncanonical_or_unsafe_provider_paths_and_cleans_workspace(tmp_path, path):
    with pytest.raises(UnsafeArchiveError):
        _snapshot([(path, b"unsafe")], workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_duplicate_paths_from_iterables_and_cleans_workspace(tmp_path):
    entries = [("safe.yaml", b"safe"), ("safe.yaml", b"replacement")]

    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        _snapshot(entries, workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "entries",
    [
        [("config.yaml", b"file"), ("config.yaml/child.yaml", b"nested")],
        [("config.yaml/child.yaml", b"nested"), ("config.yaml", b"file")],
    ],
)
def test_rejects_file_and_parent_path_conflicts(tmp_path, entries):
    with pytest.raises(UnsafeArchiveError):
        _snapshot(entries, workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_enforces_file_count_limit(tmp_path):
    with pytest.raises(SnapshotLimitError, match="file"):
        _snapshot(
            [("one.yaml", b"1"), ("two.yaml", b"2")],
            limits=SnapshotLimits(max_files=1),
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_enforces_per_file_byte_limit_including_utf8_text(tmp_path):
    with pytest.raises(SnapshotLimitError, match="file"):
        _snapshot(
            {"config.yaml": "éé"},
            limits=SnapshotLimits(max_file_bytes=3),
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_enforces_total_extracted_byte_limit(tmp_path):
    with pytest.raises(SnapshotLimitError, match="extracted"):
        _snapshot(
            [("one.yaml", b"12"), ("two.yaml", b"34")],
            limits=SnapshotLimits(max_extracted_bytes=3, max_file_bytes=3),
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_file_snapshots_do_not_apply_the_archive_compressed_byte_limit(tmp_path):
    with _snapshot(
        {"config.yaml": b"contents"},
        limits=SnapshotLimits(max_compressed_bytes=1, max_file_bytes=16, max_extracted_bytes=16),
        workspace_parent=tmp_path,
    ) as snapshot:
        assert snapshot.metadata.extracted_bytes == len(b"contents")


@pytest.mark.parametrize("content", [None, bytearray(b"content"), 42])
def test_rejects_provider_file_content_that_is_not_bytes_or_text(tmp_path, content):
    with pytest.raises(SnapshotError, match="bytes or text"):
        _snapshot([("config.yaml", content)], workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_text_that_cannot_be_encoded_as_utf8(tmp_path):
    with pytest.raises(SnapshotError, match="UTF-8"):
        _snapshot({"config.yaml": "\ud800"}, workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entries", [[("config.yaml",)], [("a", b"x", "extra")]])
def test_rejects_malformed_provider_file_entries(tmp_path, entries):
    with pytest.raises(SnapshotError, match="path-content pair"):
        _snapshot(entries, workspace_parent=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_rejects_invalid_commit_identity_before_creating_workspace(tmp_path):
    with pytest.raises(SnapshotIdentityError, match="full"):
        _snapshot({"config.yaml": b"safe"}, workspace_parent=tmp_path, commit_sha="main")

    assert list(tmp_path.iterdir()) == []


def test_close_removes_only_the_provider_snapshot_owned_workspace(tmp_path):
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    snapshot = _snapshot({"config.yaml": b"safe"}, workspace_parent=tmp_path)
    workspace = snapshot.workspace

    assert workspace.parent == tmp_path
    assert workspace.is_dir()
    snapshot.close()
    snapshot.close()

    assert not workspace.exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_failure_after_writing_a_file_cleans_only_the_partial_workspace(tmp_path):
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("preserve", encoding="utf-8")

    with pytest.raises(UnsafeArchiveError):
        _snapshot(
            [("safe.yaml", b"safe"), ("../escape.yaml", b"unsafe")],
            workspace_parent=tmp_path,
        )

    assert list(tmp_path.iterdir()) == [unrelated]
