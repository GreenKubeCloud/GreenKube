"""RECOMMENDATION_SCAN_INTERVAL setting (E1)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from greenkube.core.config import Config


def test_scan_interval_defaults_to_one_hour(monkeypatch):
    monkeypatch.delenv("RECOMMENDATION_SCAN_INTERVAL", raising=False)

    cfg = Config()

    assert cfg.RECOMMENDATION_SCAN_INTERVAL == "1h"
    assert cfg.recommendation_scan_interval == timedelta(hours=1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("2H", timedelta(hours=2)),
        ("90s", timedelta(seconds=90)),
        ("", timedelta(hours=1)),
    ],
)
def test_scan_interval_accepts_scheduler_durations(monkeypatch, raw, expected):
    monkeypatch.setenv("RECOMMENDATION_SCAN_INTERVAL", raw)

    assert Config().recommendation_scan_interval == expected


@pytest.mark.parametrize("raw", ["30", "1d", "-5m", "30s", "25h", "soon"])
def test_scan_interval_rejects_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("RECOMMENDATION_SCAN_INTERVAL", raw)

    with pytest.raises((ValidationError, ValueError)):
        Config()
