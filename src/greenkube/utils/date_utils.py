from datetime import datetime, timedelta, timezone
from typing import Optional, Union


def parse_iso_date(date_str: str) -> Optional[datetime]:
    """
    Parses an ISO 8601 date string into a datetime object.
    Handles the 'Z' suffix by replacing it with '+00:00' for compatibility
    with datetime.fromisoformat() in older Python versions (pre-3.11).

    Args:
        date_str: The ISO date string to parse.

    Returns:
        A datetime object or None if parsing fails.
    """
    if not date_str:
        return None

    try:
        # Handle Z suffix for UTC
        if date_str.endswith("Z"):
            date_str = date_str.replace("Z", "+00:00")

        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def ensure_utc(dt: Union[datetime, str]) -> datetime:
    """
    Ensures a datetime object is timezone-aware and in UTC.
    If input is a string, it parses it first.
    If input is naive, it assumes UTC.
    """
    if isinstance(dt, str):
        parsed = parse_iso_date(dt)
        if not parsed:
            raise ValueError(f"Invalid date string: {dt}")
        dt = parsed

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def to_iso_z(dt: datetime) -> str:
    """
    Converts a datetime to an ISO 8601 string with 'Z' suffix for UTC.
    """
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def parse_duration(value: str) -> timedelta:
    """Parse a human-readable duration string into a :class:`~datetime.timedelta`.

    Supported formats: ``'10min'``, ``'2h'``, ``'7d'``, ``'3w'``, ``'1m'`` (month ≈ 30 d),
    ``'1y'`` (year ≈ 365 d).

    Args:
        value: The duration string to parse.

    Returns:
        A timedelta corresponding to the parsed duration.

    Raises:
        ValueError: If *value* does not match a recognised format.
    """
    import re

    match = re.match(r"^(\d+)(min|[hdwmy])$", value.lower())
    if not match:
        raise ValueError(
            f"Invalid duration format: '{value}'. Use format like '10min', '2h', '7d', '3w', '1m' (month), '1y'."
        )

    amount, unit = int(match.group(1)), match.group(2)
    mapping = {
        "min": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
        "m": timedelta(days=amount * 30),
        "y": timedelta(days=amount * 365),
    }
    return mapping[unit]


def time_range_from_last(
    last: Optional[str],
    default: timedelta = timedelta(days=1),
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Compute a UTC ``(start, end)`` range from a dashboard ``last`` value.

    ``last`` accepts the same duration strings as :func:`parse_duration`, plus
    the special ``"ytd"`` slug used by the frontend dashboard to mean year to
    date. When ``last`` is empty, ``default`` is used.

    Args:
        last: Duration string such as ``"24h"`` or the special ``"ytd"`` slug.
        default: Duration used when ``last`` is not provided.
        now: Optional current time override, mainly for tests.

    Returns:
        A timezone-aware UTC ``(start, end)`` tuple.

    Raises:
        ValueError: If ``last`` is neither ``"ytd"`` nor a valid duration.
    """
    end = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    if not last:
        return end - default, end

    window = last.strip()
    if window.lower() == "ytd":
        return datetime(end.year, 1, 1, tzinfo=timezone.utc), end

    return end - parse_duration(window), end
