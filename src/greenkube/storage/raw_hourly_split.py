# src/greenkube/storage/raw_hourly_split.py
"""Split a read window between the raw and the hourly metric tables (ROL-01).

The compression watermark (``metrics_compression_watermarks.compressed_until``)
is the single boundary between the two tables: every hour below it has its
complete aggregate in ``*_hourly``, every row at or after it is still raw.
Readers must never split at wall-clock ``now - METRICS_COMPRESSION_AGE_HOURS``:
the hours between the watermark and that cutoff (the current hour, every hour
the compressor did not reach yet) would be read from neither table.

Rule used by every reader:

* the **hourly branch** reads only buckets that lie fully inside the window and
  below the boundary: ``ceil_hour(start) <= hour_bucket < min(boundary, floor_hour(end))``;
* the **raw branch** reads every other instant of the window:
  ``start <= timestamp < end AND NOT (hourly_start <= timestamp < hourly_end)``.
  The partial first and last hours of a window that is not hour-aligned are
  therefore read from raw rows. Raw rows exist below the boundary until
  ``METRICS_RAW_RETENTION_DAYS``; a window edge older than that loses its
  partial hour (at most one hour per edge), it is never counted twice.

Each raw row and each hourly row is thus read at most once, and every raw row
of the window is covered either by itself or by the aggregate of its hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

#: Boundary returned when only hourly rows can exist (no watermark, no raw row).
ALL_HOURLY = datetime(9999, 1, 1, tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def floor_hour(value: datetime) -> datetime:
    """Return the UTC hour that contains ``value``."""
    return _utc(value).replace(minute=0, second=0, microsecond=0)


def ceil_hour(value: datetime) -> datetime:
    """Return the first UTC hour boundary at or after ``value``."""
    floored = floor_hour(value)
    return floored if floored == _utc(value) else floored + timedelta(hours=1)


@dataclass(frozen=True)
class RawHourlySplit:
    """How one read window ``[start, end)`` is shared between raw and hourly rows.

    ``[hourly_start, hourly_end)`` are the full hours read from the hourly
    table; it is empty (both equal to ``start``) when no hour is read there.
    """

    start: datetime
    end: datetime
    hourly_start: datetime
    hourly_end: datetime

    @property
    def has_hourly(self) -> bool:
        """Whether the hourly branch reads at least one bucket."""
        return self.hourly_start < self.hourly_end

    @property
    def raw_ranges(self) -> tuple[tuple[datetime, datetime], ...]:
        """Half-open raw ranges of the window outside the hourly branch (at most two)."""
        if not self.has_hourly:
            return ((self.start, self.end),) if self.start < self.end else ()
        ranges = []
        if self.start < self.hourly_start:
            ranges.append((self.start, self.hourly_start))
        if self.hourly_end < self.end:
            ranges.append((self.hourly_end, self.end))
        return tuple(ranges)

    @property
    def has_raw(self) -> bool:
        """Whether the raw branch has a non-empty range."""
        return bool(self.raw_ranges)

    def raw_params(self) -> tuple[datetime, datetime, datetime, datetime]:
        """Parameters of ``ts >= ? AND ts < ? AND NOT (ts >= ? AND ts < ?)``."""
        return (self.start, self.end, self.hourly_start, self.hourly_end)

    def hourly_params(self) -> tuple[datetime, datetime]:
        """Parameters of ``hour_bucket >= ? AND hour_bucket < ?``."""
        return (self.hourly_start, self.hourly_end)


def raw_predicate(column: str, first: str, second: str, third: str, fourth: str) -> str:
    """SQL predicate of the raw branch for the given placeholders (see :meth:`RawHourlySplit.raw_params`)."""
    return f"{column} >= {first} AND {column} < {second} AND NOT ({column} >= {third} AND {column} < {fourth})"


def split_window(start: datetime, end: datetime, boundary: Optional[datetime]) -> RawHourlySplit:
    """Split ``[start, end)`` at the hour-aligned compression ``boundary``.

    ``boundary`` ``None`` reads everything from raw rows.
    """
    start, end = _utc(start), _utc(end)
    hourly_start = hourly_end = start
    if boundary is not None and start < end:
        first = ceil_hour(start)
        last = min(floor_hour(boundary), floor_hour(end))
        if first < last:
            hourly_start, hourly_end = first, last
    return RawHourlySplit(start, end, hourly_start, hourly_end)


def _parse(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


async def read_compression_boundary(db_manager: Any, raw_table: str, hourly_table: str) -> datetime:
    """Return the raw/hourly boundary of ``raw_table``: its compression watermark.

    Before the first compression (no watermark row) the boundary is the first
    raw hour, the same rule the compressor uses to resume: hourly rows below it
    (an install upgraded from the legacy compressor) are read, raw rows are
    never hidden. A first raw hour already partially pruned whose aggregate
    exists is read from the hourly table. Without any raw row everything is
    read from the hourly table.
    """
    timestamp = '"timestamp"' if db_manager.db_type != "postgres" else "timestamp"
    async with db_manager.connection_scope() as conn:
        if db_manager.db_type == "postgres":
            watermark = await conn.fetchval(
                "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = $1", raw_table
            )
            if watermark is not None:
                return floor_hour(watermark)
            first_raw = _parse(await conn.fetchval(f"SELECT MIN({timestamp}) FROM {raw_table}"))
            if first_raw is None:
                return ALL_HOURLY
            lower = floor_hour(first_raw)
            if first_raw > lower and await conn.fetchval(
                f"SELECT 1 FROM {hourly_table} WHERE hour_bucket = $1 LIMIT 1", lower
            ):
                lower += timedelta(hours=1)
            return lower
        async with conn.execute(
            "SELECT compressed_until FROM metrics_compression_watermarks WHERE table_name = ?", (raw_table,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return floor_hour(_parse(row[0]) or ALL_HOURLY)
        async with conn.execute(f"SELECT MIN({timestamp}) FROM {raw_table}") as cursor:
            row = await cursor.fetchone()
        first_raw = _parse(row[0]) if row else None
        if first_raw is None:
            return ALL_HOURLY
        lower = floor_hour(first_raw)
        if first_raw > lower:
            from ..utils.date_utils import to_iso_z

            async with conn.execute(
                f"SELECT 1 FROM {hourly_table} WHERE hour_bucket = ? LIMIT 1", (to_iso_z(lower),)
            ) as cursor:
                if await cursor.fetchone() is not None:
                    lower += timedelta(hours=1)
        return lower
