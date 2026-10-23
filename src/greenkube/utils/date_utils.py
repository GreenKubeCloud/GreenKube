"""Backward-compatible date utility exports.

New code should import shared time semantics from :mod:`greenkube.utils.time`.
"""

from datetime import timedelta

from .time import (
    ensure_utc,
    parse_duration,
    parse_iso_date,
    time_range_from_last,
    to_iso_z,
)

__all__ = ["ensure_utc", "parse_duration", "parse_iso_date", "time_range_from_last", "to_iso_z", "timedelta"]
