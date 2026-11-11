# tests/core/test_scheduler.py
"""Interval helpers of the controller loops (the ``Scheduler`` class was removed, M8)."""

import pytest

import greenkube.core.scheduler as scheduler
from greenkube.core.scheduler import parse_interval_seconds, seconds_until_boundary


@pytest.mark.parametrize(("text", "seconds"), [("30s", 30), ("5m", 300), ("1h", 3600), ("2H", 7200)])
def test_parse_interval_seconds(text, seconds):
    assert parse_interval_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "5d", "m5", "-1m"])
def test_parse_interval_seconds_rejects_invalid_formats(text):
    with pytest.raises(ValueError, match="Invalid interval format"):
        parse_interval_seconds(text)


def test_parse_interval_seconds_rejects_zero():
    with pytest.raises(ValueError, match="greater than zero"):
        parse_interval_seconds("0m")


def test_seconds_until_boundary_targets_step_plus_offset():
    # 10:06:20 UTC with a 5-minute step and a 60s offset -> next run at 10:11:00.
    now = 1_762_509_980.0  # 2025-11-07T10:06:20Z
    assert seconds_until_boundary(300, 60, now=now) == pytest.approx(280.0)
    # Exactly on the boundary waits a full interval (never a zero-second hot loop).
    assert seconds_until_boundary(300, 60, now=1_762_509_960.0) == pytest.approx(300.0)


def test_the_scheduler_class_is_gone():
    assert not hasattr(scheduler, "Scheduler")
