from datetime import datetime, timedelta, timezone

import pytest

from greenkube.utils.date_utils import ensure_utc, parse_duration, parse_iso_date, time_range_from_last, to_iso_z


def test_parse_iso_date_normalizes_naive_and_offset_values_to_utc():
    assert parse_iso_date("2026-01-01T12:00:00") == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert parse_iso_date("2026-01-01T13:00:00+01:00") == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert parse_iso_date("not-a-date") is None


def test_utc_serialization_and_duration_parsing():
    value = datetime(2026, 1, 1, 13, tzinfo=timezone(timedelta(hours=1)))
    assert ensure_utc(value).hour == 12
    assert to_iso_z(value) == "2026-01-01T12:00:00Z"
    assert parse_duration("2h") == timedelta(hours=2)


def test_time_range_from_last_uses_utc_and_half_open_order():
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    start, end = time_range_from_last("24h", now=now)
    assert (start, end) == (datetime(2026, 9, 28, 12, tzinfo=timezone.utc), now)
    assert start < end


def test_invalid_duration_is_explicit():
    with pytest.raises(ValueError):
        parse_duration("invalid")
