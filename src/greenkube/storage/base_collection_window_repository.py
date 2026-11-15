"""Repository contract for the per-step collection record (COL-04).

Every step of every collected, empty, failed or abandoned window has a row in
``collection_windows``, so a period with holes is detectably incomplete: the
coverage of ``[start, end)`` is the share of its steps that were collected
(with rows) or confirmed empty (Prometheus was scraping, no pod ran).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from greenkube.utils.time import ensure_utc

#: Rows were written for the step.
STATUS_COLLECTED = "collected"
#: Prometheus was scraping during the step, but no pod produced a row.
STATUS_EMPTY = "empty"
#: Prometheus had no scrape data for the step; it is retried while inside the backfill horizon.
STATUS_FAILED = "failed"
#: The step was never collected and never will be (older than the backfill horizon).
STATUS_GAP = "gap"
#: No record exists for the step (data collected before COL-04, or never planned).
STATUS_UNKNOWN = "unknown"
WINDOW_STATUSES = (STATUS_COLLECTED, STATUS_EMPTY, STATUS_FAILED, STATUS_GAP)

#: Reason of a step without scrape data (the cAdvisor probe returned nothing).
REASON_NO_SCRAPE_DATA = "no_scrape_data"
#: Reason of a step abandoned because it left ``COLLECTION_MAX_BACKFILL`` before it was collected.
REASON_BEYOND_BACKFILL = "beyond_backfill_horizon"
#: Reason of a step recorded from the rows already stored when the table was introduced.
REASON_SEEDED = "seeded_from_stored_rows"
#: Reason of a collected step without container rows because cAdvisor exports pod cgroups only (COL-05).
REASON_POD_LEVEL_ONLY = "pod_level_usage_only"
#: Reason of an empty step whose range processor cannot probe Prometheus.
REASON_NOT_PROBED = "not_probed"
#: Reason of a collected step of a window that left invalid rows out (partial window, STO-03).
REASON_ROWS_REJECTED = "rows_rejected"


@dataclass(frozen=True)
class CollectionWindowRecord:
    """Outcome of ``[window_start, window_end)``: one step, or a range of abandoned steps."""

    cluster_uid: str
    source: str
    window_start: datetime
    window_end: datetime
    status: str
    expected_steps: int
    steps_with_data: int = 0
    pod_rows: int = 0
    cpu_series: int = 0
    pods_without_cpu: int = 0
    reason: str | None = None
    opencost_total_cost: float | None = None
    attributed_cost: float | None = None
    collected_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status not in WINDOW_STATUSES:
            raise ValueError(f"Unknown collection window status {self.status!r}.")
        object.__setattr__(self, "window_start", ensure_utc(self.window_start))
        object.__setattr__(self, "window_end", ensure_utc(self.window_end))


@dataclass(frozen=True)
class CoverageHole:
    """Consecutive steps of one status that are not covered."""

    start: datetime
    end: datetime
    status: str
    reason: str | None


@dataclass(frozen=True)
class CollectionCoverage:
    """Coverage of a period: how many of its steps hold known data."""

    start: datetime
    end: datetime
    step_seconds: int
    expected_steps: int
    collected_steps: int
    empty_steps: int
    failed_steps: int
    gap_steps: int
    unknown_steps: int
    holes: list[CoverageHole] = field(default_factory=list)

    @property
    def covered_steps(self) -> int:
        """Steps whose data is known: collected, or confirmed empty."""
        return self.collected_steps + self.empty_steps

    @property
    def ratio(self) -> float:
        """Covered steps over expected steps (1.0 for an empty period)."""
        return self.covered_steps / self.expected_steps if self.expected_steps else 1.0

    @property
    def complete(self) -> bool:
        """True when every step of the period is covered."""
        return self.covered_steps == self.expected_steps


def compute_coverage(
    records: list[CollectionWindowRecord], start: datetime, end: datetime, step: timedelta
) -> CollectionCoverage:
    """Coverage of the step-aligned period ``[start, end)`` from overlapping records.

    A range record (several steps, e.g. an abandoned gap) is expanded over the
    period; a single-step record of the same step wins over it (a step of the
    gap re-collected later with ``--last``).  Steps without any record are
    ``unknown``.
    """
    start = ensure_utc(start)
    end = ensure_utc(end)
    step_seconds = int(step.total_seconds())
    if step_seconds <= 0:
        raise ValueError("Coverage step must be positive.")
    first = int(start.timestamp()) // step_seconds * step_seconds
    last = int(end.timestamp())
    steps = list(range(first, last, step_seconds)) if last > first else []
    status: dict[int, tuple[str, str | None]] = {}
    ordered = sorted(records, key=lambda record: record.expected_steps != 1)
    # Range rows first, then single steps, so single steps override ranges.
    for record in reversed(ordered):
        record_start = int(record.window_start.timestamp())
        record_end = int(record.window_end.timestamp())
        if record.expected_steps == 1:
            if first <= record_start < last:
                status[record_start] = (record.status, record.reason)
            continue
        cursor = max(record_start, first)
        cursor = cursor + (-(cursor - record_start) % step_seconds)
        while cursor < min(record_end, last):
            status[cursor] = (record.status, record.reason)
            cursor += step_seconds
    counts = dict.fromkeys((*WINDOW_STATUSES, STATUS_UNKNOWN), 0)
    holes: list[CoverageHole] = []
    for ts in steps:
        step_status, reason = status.get(ts, (STATUS_UNKNOWN, None))
        counts[step_status] += 1
        if step_status in (STATUS_COLLECTED, STATUS_EMPTY):
            continue
        hole_start = datetime.fromtimestamp(ts, tz=start.tzinfo)
        hole_end = datetime.fromtimestamp(ts + step_seconds, tz=start.tzinfo)
        previous = holes[-1] if holes else None
        if previous and previous.end == hole_start and previous.status == step_status and previous.reason == reason:
            holes[-1] = CoverageHole(previous.start, hole_end, step_status, reason)
        else:
            holes.append(CoverageHole(hole_start, hole_end, step_status, reason))
    return CollectionCoverage(
        start=start,
        end=end,
        step_seconds=step_seconds,
        expected_steps=len(steps),
        collected_steps=counts[STATUS_COLLECTED],
        empty_steps=counts[STATUS_EMPTY],
        failed_steps=counts[STATUS_FAILED],
        gap_steps=counts[STATUS_GAP],
        unknown_steps=counts[STATUS_UNKNOWN],
        holes=holes,
    )


class CollectionWindowRepository(ABC):
    """Stores the outcome of every collection step per ``(cluster_uid, source)``."""

    @abstractmethod
    async def record(self, records: list[CollectionWindowRecord], *, connection: Any = None) -> int:
        """Upsert records keyed by ``(cluster_uid, source, window_start)``; returns the number written.

        On ``connection`` (inside the caller's transaction, not committed) when
        given, so a window's rows and its records commit together (STO-03).
        """

    @abstractmethod
    async def list_overlapping(
        self, cluster_uid: str, source: str, start: datetime, end: datetime
    ) -> list[CollectionWindowRecord]:
        """Records whose ``[window_start, window_end)`` overlaps ``[start, end)``, oldest first."""

    @abstractmethod
    async def list_failed(
        self, cluster_uid: str, source: str, since: datetime, limit: int
    ) -> list[CollectionWindowRecord]:
        """Failed steps starting at or after ``since``, least recently attempted first."""

    @abstractmethod
    async def expire_failed(self, cluster_uid: str, source: str, before: datetime) -> int:
        """Turn failed steps starting before ``before`` into gaps (they can no longer be retried)."""

    @abstractmethod
    async def last_hole(self, cluster_uid: str, source: str) -> CollectionWindowRecord | None:
        """The most recent gap or failed record, or ``None``."""

    @abstractmethod
    async def has_records(self, cluster_uid: str, source: str) -> bool:
        """True when at least one record exists for the cluster and source."""

    @abstractmethod
    async def stored_metric_timestamps(self, since: datetime | None = None) -> list[datetime]:
        """Distinct timestamps of the stored pod rows (``combined_metrics``), to seed the table."""


def create_collection_window_repository(db_manager: Any, db_type: str) -> CollectionWindowRepository:
    """Build the collection window repository matching ``DB_TYPE``."""
    if db_type == "postgres":
        from .postgres.collection_window_repository import PostgresCollectionWindowRepository

        return PostgresCollectionWindowRepository(db_manager)
    if db_type == "sqlite":
        from .sqlite.collection_window_repository import SQLiteCollectionWindowRepository

        return SQLiteCollectionWindowRepository(db_manager)
    raise NotImplementedError(f"CollectionWindowRepository for DB_TYPE '{db_type}' not implemented.")
