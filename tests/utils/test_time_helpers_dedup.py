# tests/utils/test_time_helpers_dedup.py
"""One UTC normalizer and one commit-SHA validator for the whole code base (roadmap M6)."""

import ast
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

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
