from datetime import datetime, timezone

import pytest

from greenkube.core.historical_range_processor import HistoricalRangeProcessor
from greenkube.core.optimization.statistics import SECONDS_PER_YEAR, annualized_window_total
from greenkube.utils.time import TimePeriod, parse_query_step_seconds


def test_query_step_parser_has_one_shared_default():
    assert parse_query_step_seconds("5m") == 300
    assert parse_query_step_seconds("invalid") == 300
    assert HistoricalRangeProcessor._parse_duration_to_seconds("invalid") == 300


def test_time_period_normalizes_utc_and_exposes_duration():
    period = TimePeriod(
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
    )
    assert period.start.tzinfo == timezone.utc
    assert period.duration_seconds == 3600


def test_time_period_rejects_empty_or_reversed_ranges():
    value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        TimePeriod(value, value)
    with pytest.raises(ValueError):
        TimePeriod(value, datetime(2025, 1, 1, tzinfo=timezone.utc))


def test_annualization_uses_shared_constant():
    assert SECONDS_PER_YEAR == 365 * 24 * 60 * 60
    assert annualized_window_total(1.0, SECONDS_PER_YEAR) == 1.0
