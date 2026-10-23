"""Shared time semantics for GreenKube.

All public datetimes returned by this module are timezone-aware and normalized
to UTC. Time periods use the half-open convention ``[start, end)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeAlias

UTCDateTime: TypeAlias = datetime
UTC = timezone.utc
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
DEFAULT_QUERY_STEP_SECONDS = 300


def ensure_utc(value: UTCDateTime | str) -> UTCDateTime:
    """Return an aware UTC datetime, interpreting naive values as UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid date string: {value}") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_iso_date(value: str) -> UTCDateTime | None:
    """Parse an ISO-8601 string into an aware UTC datetime."""
    if not value:
        return None
    try:
        return ensure_utc(value)
    except ValueError:
        return None


def to_iso_z(value: UTCDateTime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string with a ``Z`` suffix."""
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_duration(value: str) -> timedelta:
    """Parse dashboard durations such as ``5m``, ``2h``, ``7d``, or ``1y``."""
    match = re.fullmatch(r"(\d+)(min|[hdwmy])", value.strip().lower())
    if not match:
        raise ValueError(
            f"Invalid duration format: '{value}'. Use format like '10min', '2h', '7d', '3w', '1m' (month), '1y'."
        )
    amount, unit = int(match.group(1)), match.group(2)
    return {
        "min": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
        "m": timedelta(days=amount * 30),
        "y": timedelta(days=amount * 365),
    }[unit]


def parse_query_step_seconds(value: str, *, default: int = DEFAULT_QUERY_STEP_SECONDS) -> int:
    """Parse a Prometheus ``s``/``m``/``h`` step, using one explicit default."""
    match = re.fullmatch(r"(\d+)([smh])", str(value).strip().lower())
    if not match:
        return default
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
    return seconds if seconds > 0 else default


@dataclass(frozen=True, slots=True)
class TimePeriod:
    """A validated half-open UTC period ``[start, end)``."""

    start: UTCDateTime
    end: UTCDateTime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start)
        end = ensure_utc(self.end)
        if end <= start:
            raise ValueError("Time period end must be after start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def time_range_from_last(
    last: str | None,
    default: timedelta = timedelta(days=1),
    now: UTCDateTime | None = None,
) -> tuple[UTCDateTime, UTCDateTime]:
    """Return a UTC half-open range for a dashboard duration or ``ytd``."""
    end = ensure_utc(now) if now is not None else datetime.now(UTC)
    if not last:
        start = end - default
    elif last.strip().lower() == "ytd":
        start = datetime(end.year, 1, 1, tzinfo=UTC)
    else:
        start = end - parse_duration(last)
    return TimePeriod(start, end).start, end
