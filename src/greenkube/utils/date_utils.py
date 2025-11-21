from datetime import datetime, timezone
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
