# tests/utils/test_shared_helpers.py
"""One UTC normalizer and one commit-SHA validator for the whole code base (roadmap M6)."""

import ast
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from greenkube.gitops.sha import SHA256_DIGEST, is_full_commit_sha, revision_commit
from greenkube.utils.time import ensure_utc

SRC = Path(__file__).resolve().parents[2] / "src" / "greenkube"


class _NoOffset(tzinfo):
    """A tzinfo that does not know its offset: Python treats such values as naive."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def test_ensure_utc_treats_unknown_offsets_as_utc():
    value = datetime(2026, 1, 1, 12, tzinfo=_NoOffset())

    assert ensure_utc(value) == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def test_ensure_utc_converts_aware_values():
    paris = timezone(timedelta(hours=1))

    assert ensure_utc(datetime(2026, 1, 1, 13, tzinfo=paris)) == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", ["_as_utc", "_aware_utc", "_as_utc_datetime"])
def test_no_module_defines_its_own_utc_normalizer(name):
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if any(
            isinstance(node, ast.FunctionDef) and node.name == name for node in ast.walk(ast.parse(path.read_text()))
        )
    ]

    assert offenders == []


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a" * 40, True),
        ("A" * 64, True),
        ("a" * 39, False),
        ("a" * 41, False),
        ("a" * 40 + "\n", False),
        ("sha1:" + "a" * 40, False),
        (None, False),
    ],
)
def test_one_full_commit_sha_definition(value, valid):
    assert is_full_commit_sha(value) is valid


@pytest.mark.parametrize(
    ("revision", "commit"),
    [
        ("A" * 40, "a" * 40),
        ("main@sha1:" + "B" * 40, "b" * 40),
        ("sha256:" + "c" * 64, "c" * 64),
        ("main", None),
        ("main@sha1:" + "b" * 39, None),
    ],
)
def test_revision_commit_extracts_the_full_commit(revision, commit):
    assert revision_commit(revision) == commit


def test_digests_are_lowercase_sha256_only():
    assert SHA256_DIGEST.fullmatch("sha256:" + "a" * 64)
    assert not SHA256_DIGEST.fullmatch("sha256:" + "A" * 64)
    assert not SHA256_DIGEST.fullmatch("sha1:" + "a" * 40)


def test_git_object_ids_are_not_redefined_outside_the_shared_module():
    shared = SRC / "gitops" / "sha.py"
    pattern_sources = (r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", r"sha256:[0-9a-f]{64}")
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path != shared and any(source in path.read_text() for source in pattern_sources)
    ]

    assert offenders == []
