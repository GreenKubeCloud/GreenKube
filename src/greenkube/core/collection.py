# src/greenkube/core/collection.py
"""Gap-aware metric collection: half-open windows, watermarks and catch-up.

A :class:`CollectionWindow` is a half-open, step-aligned interval
``[start, end)``.  A stored row with timestamp ``T`` and ``duration_seconds =
step`` describes usage during ``[T, T + step)``.

The scheduled job reads the per-source watermark (the exclusive end of the
last window that was fully written), plans every missing window up to
``now - COLLECTION_SAFETY_LAG`` (bounded by ``COLLECTION_MAX_BACKFILL``),
collects them oldest first and advances the watermark only after the rows of
a window have been written.  Re-running a window is an idempotent upsert.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from ..collectors.promql import parse_duration_seconds
from ..models.metrics import CombinedMetric
from ..models.provenance import ALLOCATED_COST_SOURCES
from ..storage.base_collection_watermark_repository import CollectionWatermark, CollectionWatermarkRepository
from ..storage.base_collection_window_repository import (
    COST_STATUS_PENDING,
    COST_STATUS_PENDING_EXPIRED,
    REASON_BEYOND_BACKFILL,
    REASON_NO_SCRAPE_DATA,
    REASON_NOT_PROBED,
    REASON_POD_LEVEL_ONLY,
    REASON_RECOLLECT_SKIPPED,
    REASON_ROWS_REJECTED,
    REASON_SEEDED,
    STATUS_COLLECTED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
    CollectionWindowRepository,
    compute_coverage,
)
from ..utils.time import ensure_utc
from . import collection_telemetry
from .leader_election import LeaderFence, LeadershipLostError, current_leader_fence

logger = logging.getLogger(__name__)

#: Watermark source written by the combined (pod + container) metrics job.
METRICS_WATERMARK_SOURCE = "combined_metrics"
#: Watermark row recording the end of the last window OpenCost priced for this cluster: the fact
#: "OpenCost is installed here" outlives the window records of the horizon (COST-01 review 2).
OPENCOST_SEEN_SOURCE = "opencost_cost"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TARGET_CHUNK_SECONDS = 3600
#: Unattributed share of the OpenCost window total above which a warning is logged (COL-03 step 4).
_UNATTRIBUTED_COST_WARNING = 0.01
#: The scrapes per rate window are measured at most this often (COL-04 step 3).
_SCRAPE_CHECK_INTERVAL = timedelta(hours=1)
#: (cluster, source) -> time of the last scrape interval check, per process (jobs are rebuilt every tick).
_SCRAPE_CHECKS: dict[tuple[str, str], datetime] = {}


def align_down(value: datetime, step: timedelta) -> datetime:
    """Floor an aware datetime to a multiple of ``step`` since the Unix epoch."""
    step_us = _step_microseconds(step)
    offset = (ensure_utc(value) - _EPOCH) // timedelta(microseconds=1)
    return _EPOCH + timedelta(microseconds=(offset // step_us) * step_us)


def align_up(value: datetime, step: timedelta) -> datetime:
    floored = align_down(value, step)
    return floored if floored == ensure_utc(value) else floored + step


def _step_microseconds(step: timedelta) -> int:
    step_us = step // timedelta(microseconds=1)
    if step_us <= 0:
        raise ValueError("Collection step must be positive.")
    return step_us


@dataclass(frozen=True)
class CollectionWindow:
    """Half-open collection window ``[start, end)`` aligned to ``step``."""

    start: datetime  # inclusive, aligned to step
    end: datetime  # exclusive
    step: timedelta

    def __post_init__(self) -> None:
        start = ensure_utc(self.start)
        end = ensure_utc(self.end)
        step_us = _step_microseconds(self.step)
        if end <= start:
            raise ValueError("Collection window end must be after start.")
        if align_down(start, self.step) != start:
            raise ValueError(f"Collection window start {start.isoformat()} is not aligned to {self.step}.")
        if ((end - start) // timedelta(microseconds=1)) % step_us:
            raise ValueError("Collection window length must be a whole number of steps.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def covering(cls, start: datetime, end: datetime, step: timedelta) -> "CollectionWindow":
        """Return the smallest aligned window that contains ``[start, end)``."""
        aligned_start = align_down(start, step)
        aligned_end = align_up(end, step)
        if aligned_end <= aligned_start:
            aligned_end = aligned_start + step
        return cls(aligned_start, aligned_end, step)

    @property
    def step_seconds(self) -> int:
        return int(self.step.total_seconds())

    @property
    def steps(self) -> int:
        return int((self.end - self.start) / self.step)

    @property
    def first_evaluation(self) -> datetime:
        """Prometheus evaluation time of the first step (rates look back one step)."""
        return self.start + self.step

    @property
    def last_evaluation(self) -> datetime:
        return self.end

    def sample_starts(self) -> Iterator[datetime]:
        current = self.start
        while current < self.end:
            yield current
            current += self.step

    def contains(self, value: datetime) -> bool:
        return self.start <= ensure_utc(value) < self.end

    def split(self, max_steps: int) -> list["CollectionWindow"]:
        """Split into consecutive windows of at most ``max_steps`` steps (oldest first)."""
        if max_steps <= 0:
            raise ValueError("max_steps must be positive.")
        chunks: list[CollectionWindow] = []
        current = self.start
        while current < self.end:
            chunk_end = min(current + self.step * max_steps, self.end)
            chunks.append(CollectionWindow(current, chunk_end, self.step))
            current = chunk_end
        return chunks


@dataclass(frozen=True)
class CollectionPlan:
    """Windows to collect for one job run."""

    windows: tuple[CollectionWindow, ...]
    gap_windows: int = 0
    horizon_start: datetime | None = None


def plan_collection(
    *,
    watermark: datetime | None,
    now: datetime,
    step: timedelta,
    safety_lag: timedelta,
    max_backfill: timedelta,
    max_chunk_steps: int,
) -> CollectionPlan:
    """Plan ``[watermark, now - safety_lag)`` in step-aligned chunks, oldest first.

    Without a watermark (first run, or upgrade from a release without
    watermarks) the whole backfill horizon is collected.  Windows older than
    the horizon are abandoned and reported as gap windows.
    """
    end = align_down(ensure_utc(now) - safety_lag, step)
    horizon_start = align_down(ensure_utc(now) - max_backfill, step)
    gap_windows = 0
    if watermark is None:
        start = horizon_start
    else:
        start = align_down(ensure_utc(watermark), step)
        if start < horizon_start:
            gap_windows = int((horizon_start - start) / step)
            start = horizon_start
    if start >= end:
        return CollectionPlan(windows=(), gap_windows=gap_windows, horizon_start=horizon_start)
    windows = CollectionWindow(start, end, step).split(max_chunk_steps)
    return CollectionPlan(windows=tuple(windows), gap_windows=gap_windows, horizon_start=horizon_start)


def collection_cluster_key(config: Any) -> str:
    """Key of the watermark and of the collection window records for this cluster."""
    return getattr(config, "CLUSTER_NAME", "") or "default"


def collection_step(config: Any) -> timedelta:
    """The collection step (``PROMETHEUS_QUERY_RANGE_STEP``)."""
    return timedelta(
        seconds=parse_duration_seconds(
            getattr(config, "PROMETHEUS_QUERY_RANGE_STEP", "5m"), field_name="PROMETHEUS_QUERY_RANGE_STEP"
        )
    )


def default_chunk_steps(step: timedelta) -> int:
    """About one hour of steps per ``run_range`` call (at least one step)."""
    return max(1, _TARGET_CHUNK_SECONDS // max(int(step.total_seconds()), 1))


class RangeProcessor(Protocol):
    async def run_range(self, start: Any, end: Any, step: Any = None, namespace: Any = None) -> list[CombinedMetric]:
        """Collect rows for every step of ``[start, end)``."""
        ...


class MetricsWriter(Protocol):
    async def write_combined_metrics(
        self,
        metrics: list[CombinedMetric],
        *,
        replace_containers: bool = False,
        window: tuple[datetime, datetime] | None = None,
        run_id: str | None = None,
    ) -> int:
        """Upsert combined and container rows; ``replace_containers`` rewrites the container rows of each pod."""
        ...

    async def write_window(
        self,
        rows: list[CombinedMetric],
        idle_rows: list[Any],
        *,
        window: tuple[datetime, datetime],
        run_id: str,
        on_commit: Callable[[Any], Awaitable[Any]] | None = None,
        force: bool = False,
    ) -> int:
        """Write pod, container and idle rows of a window and run ``on_commit`` in one transaction (STO-03)."""
        ...


@dataclass
class CollectionReport:
    """Outcome of one :meth:`MetricsCollectionJob.run`."""

    windows_planned: int = 0
    windows_collected: int = 0
    rows_written: int = 0
    gap_windows: int = 0
    #: Steps recorded as failed (no scrape data) in this run; they are retried while inside the horizon.
    failed_steps: int = 0
    #: Steps recorded as empty (Prometheus scraped, no pod row) in this run.
    empty_steps: int = 0
    #: Failed steps of earlier runs re-collected with data in this run.
    recovered_steps: int = 0
    #: Coverage of the backfill horizon after the run (``None`` without a window repository).
    coverage_ratio: float | None = None
    #: Share of the OpenCost total of the last written window that no stored row carries (COL-03 step 4).
    cost_unattributed_ratio: float | None = None
    #: Most scrapes of a cAdvisor series inside the rate window, when measured in this run (COL-04 step 3).
    rate_window_scrapes: float | None = None
    #: Steps whose cost waited for OpenCost and were re-priced from it in this run (COST-01).
    repriced_steps: int = 0
    #: Steps inside the horizon whose cost still waits for OpenCost after the run (COST-01).
    cost_pending_steps: int | None = None
    watermark: datetime | None = None
    error: str | None = None
    #: Rows of the most recent collected sample (pod and container rows).
    latest_rows: list[CombinedMetric] = field(default_factory=list)
    #: Start of the first window planned in this run; failed steps from there on are not retried in the same run.
    planned_from: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


class MetricsCollectionJob:
    """Catch-up loop that collects every missing window and advances the watermark."""

    def __init__(
        self,
        *,
        processor: RangeProcessor,
        metrics_repository: MetricsWriter,
        watermark_repository: CollectionWatermarkRepository,
        config: Any,
        cluster_uid: str | None = None,
        source: str = METRICS_WATERMARK_SOURCE,
        clock: Callable[[], datetime] | None = None,
        fence_provider: Callable[[], LeaderFence] | None = None,
        window_repository: CollectionWindowRepository | None = None,
    ) -> None:
        self.processor = processor
        self.metrics_repository = metrics_repository
        self.watermark_repository = watermark_repository
        #: Per-step record of collected, empty, failed and abandoned steps (COL-04).
        self.window_repository = window_repository
        self.source = source
        self.cluster_uid = cluster_uid or collection_cluster_key(config)
        self.step = collection_step(config)
        self.safety_lag = timedelta(
            seconds=parse_duration_seconds(
                getattr(config, "COLLECTION_SAFETY_LAG", "1m"), field_name="COLLECTION_SAFETY_LAG"
            )
        )
        self.max_backfill = timedelta(
            seconds=parse_duration_seconds(
                getattr(config, "COLLECTION_MAX_BACKFILL", "24h"), field_name="COLLECTION_MAX_BACKFILL"
            )
        )
        self.max_chunk_steps = default_chunk_steps(self.step)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fence_provider = fence_provider or current_leader_fence

    async def run(self, last: timedelta | None = None, *, force: bool = False) -> CollectionReport:
        """Collect missing windows (or the explicit ``last`` range) oldest first.

        ``force`` (only with ``last``) replaces the stored rows of the range
        even when the new pass looks worse and whatever the stored cost source:
        the operator's way to repair wrong stored rows (STO-01).
        """
        if force and last is None:
            raise ValueError("force applies to an explicit --last re-collection only")
        report = CollectionReport()
        now = ensure_utc(self._clock())
        fence = self._fence_provider()
        watermark: CollectionWatermark | None = None
        try:
            watermark = await self.watermark_repository.get(self.cluster_uid, self.source)
            await self._seed_window_records(now)
            await self._seed_opencost_known(now)
            if watermark is None and last is None:
                watermark = await self._resume_from_stored_rows(now)
            plan = self._plan(now, watermark, last)
            report.windows_planned = len(plan.windows)
            report.planned_from = plan.windows[0].start if plan.windows else None
            report.gap_windows = plan.gap_windows
            if plan.gap_windows:
                logger.warning(
                    "Collection watermark for %s is older than COLLECTION_MAX_BACKFILL; %d window(s) left as a gap.",
                    self.source,
                    plan.gap_windows,
                )
                collection_telemetry.record_gap_windows(self.source, plan.gap_windows)
                if watermark is not None and plan.horizon_start is not None:
                    await self._record_gap(watermark.last_complete_end, plan.horizon_start, plan.gap_windows, fence)
            for window in plan.windows:
                watermark = await self._collect_window(
                    window, watermark, fence, report, forced=last is not None, force_replace=force
                )
            if last is None:
                await self._retry_failed_steps(now, fence, report)
                await self._reprice_pending_steps(now, fence, report)
                await self._check_scrape_interval(now, report)
            await self._publish_coverage(now, report)
        except LeadershipLostError as error:
            report.error = str(error)
            logger.warning("Metrics collection aborted before commit: %s", error)
            collection_telemetry.record_window_error(self.source, "leadership_lost")
        except Exception as error:
            report.error = f"{type(error).__name__}: {error}"
            logger.error("Metrics collection stopped; the watermark stays at the last complete window: %s", error)
            collection_telemetry.record_window_error(self.source, "collection_failed")
        finally:
            report.watermark = watermark.last_complete_end if watermark else None
            collection_telemetry.set_watermark_lag(self.source, now, report.watermark)
        return report

    async def _resume_from_stored_rows(self, now: datetime) -> CollectionWatermark | None:
        """Without a watermark, start after the newest stored row instead of re-collecting the horizon (COL-02).

        A lost or re-keyed watermark must not re-collect (and degrade) windows
        that already have rows.  Returns an in-memory watermark at the end of
        the newest stored step inside the backfill horizon, or ``None``.
        """
        if self.window_repository is None:
            return None
        horizon_start = align_down(now - self.max_backfill, self.step)
        stored = await self.window_repository.stored_metric_timestamps(since=horizon_start)
        latest = max((ts for ts in stored if ts < now), default=None)
        if latest is None:
            return None
        resume_at = align_down(latest, self.step) + self.step
        logger.warning(
            "No collection watermark for %s under %s; resuming after the newest stored row (%s).",
            self.source,
            self.cluster_uid,
            resume_at.isoformat(),
        )
        return CollectionWatermark(self.cluster_uid, self.source, resume_at, now)

    async def _write_window(
        self,
        window: CollectionWindow,
        rows: list[CombinedMetric],
        records: list[CollectionWindowRecord],
        fence: LeaderFence,
        *,
        advance: bool,
        force: bool = False,
    ) -> tuple[int, CollectionWatermark | None]:
        """Commit a collected window in one transaction (STO-01, STO-03).

        Pod, container and ``__idle__`` rows of the pass (one generation:
        stored rows of its steps it no longer produces are deleted), the step
        records and, when ``advance``, the watermark are written together: a
        failure anywhere leaves the database as it was. The leader fence is
        checked inside the transaction, just before the watermark moves.
        """
        idle = getattr(self.processor, "last_idle_metrics", None)
        idle_rows = list(idle) if isinstance(idle, (list, tuple)) else []
        watermark: CollectionWatermark | None = None

        async def on_commit(connection: Any) -> None:
            nonlocal watermark
            if self.window_repository is not None and records:
                await self.window_repository.record(records, connection=connection)
            if advance:
                fence.check("advance watermark")
                watermark = await self.watermark_repository.advance(
                    self.cluster_uid, self.source, window.end, connection=connection
                )

        fence.check("write metrics")
        written = await self.metrics_repository.write_window(
            rows,
            idle_rows,
            window=(window.start, window.end),
            run_id=uuid4().hex,
            on_commit=on_commit,
            force=force,
        )
        return written or 0, watermark

    def _quarantine(self, rows: list[CombinedMetric]) -> tuple[list[CombinedMetric], int]:
        """Drop rows that cannot be stored (no timestamp) and count them (STO-03 step 3).

        A row the database would reject must not fail the whole window again on
        every run: it is left out, counted in
        ``greenkube_collection_rows_rejected_total`` and the window's step
        records name the rejection.
        """
        kept = [row for row in rows if row.timestamp is not None]
        rejected = len(rows) - len(kept)
        if rejected:
            logger.warning("Left %d collected row(s) without a timestamp out of the window.", rejected)
            collection_telemetry.record_rows_rejected(
                self.source, collection_telemetry.REJECT_REASON_NO_TIMESTAMP, rejected
            )
        return kept, rejected

    async def _recollection_degradation(
        self, window: CollectionWindow, rows: list[CombinedMetric]
    ) -> tuple[str | None, dict[datetime, Any]]:
        """Why a re-collection of ``window`` must not replace its stored rows, or ``None`` (STO-01).

        A step of the new pass that misses a stored pod (gone from Prometheus
        or from the cluster), or has no allocated cost where the stored rows
        had one (OpenCost unavailable), is worse than what is stored: the
        window is then left as stored. Steps the pass has no row for are never
        replaced and are not compared. Returns the reason and the stored
        profile per step.
        """
        profile = getattr(self.metrics_repository, "stored_window_profile", None)
        if profile is None:
            return None, {}
        stored = await profile(window.start, window.end)
        if not stored:
            return None, {}
        produced: dict[datetime, list[CombinedMetric]] = {}
        for row in rows:
            if row.timestamp is not None and not getattr(row, "container_name", None):
                produced.setdefault(ensure_utc(row.timestamp), []).append(row)
        for step, step_profile in stored.items():
            step_rows = produced.get(step)
            if not step_rows:
                continue
            if step_profile.pods - {(row.namespace, row.pod_name) for row in step_rows}:
                return collection_telemetry.RECOLLECT_REASON_FEWER_PODS, stored
            if step_profile.allocated_cost_rows and not any(
                row.cost_source in ALLOCATED_COST_SOURCES for row in step_rows
            ):
                return collection_telemetry.RECOLLECT_REASON_COST_UNAVAILABLE, stored
        return None, stored

    async def _record_skipped(
        self,
        window: CollectionWindow,
        rows: list[CombinedMetric],
        stored: dict[datetime, Any],
        reason: str,
    ) -> None:
        """Log, count and record a re-collection left as stored (step reason ``recollect_skipped``)."""
        logger.warning(
            "Re-collection of %s -> %s is worse than the stored rows (%s); the window is left as stored. "
            "Use --last with --force to replace it anyway.",
            window.start.isoformat(),
            window.end.isoformat(),
            reason,
        )
        collection_telemetry.record_recollect_skipped(self.source, reason)
        if self.window_repository is None:
            return
        records = [
            replace(
                record,
                status=STATUS_COLLECTED,
                reason=REASON_RECOLLECT_SKIPPED,
                steps_with_data=1,
                pod_rows=len(stored[record.window_start].pods),
                # The step keeps the cost status of its stored rows (COST-01).
                cost_status=COST_STATUS_PENDING
                if getattr(stored[record.window_start], "pending_cost_rows", 0)
                else None,
            )
            for record in await self._window_records(window, rows)
            if record.window_start in stored
        ]
        if records:
            await self.window_repository.record(records)

    def _plan(self, now: datetime, watermark: CollectionWatermark | None, last: timedelta | None) -> CollectionPlan:
        if last is None:
            return plan_collection(
                watermark=watermark.last_complete_end if watermark else None,
                now=now,
                step=self.step,
                safety_lag=self.safety_lag,
                max_backfill=self.max_backfill,
                max_chunk_steps=self.max_chunk_steps,
            )
        end = align_down(now - self.safety_lag, self.step)
        start = min(align_down(now - last, self.step), end - self.step)
        windows = CollectionWindow(start, end, self.step).split(self.max_chunk_steps)
        return CollectionPlan(windows=tuple(windows))

    async def _collect_window(
        self,
        window: CollectionWindow,
        watermark: CollectionWatermark | None,
        fence: LeaderFence,
        report: CollectionReport,
        *,
        forced: bool,
        force_replace: bool = False,
    ) -> CollectionWatermark | None:
        started = monotonic()
        fence.check("collect metrics")
        rows, rejected = self._quarantine(await self.processor.run_range(start=window.start, end=window.end))
        contiguous = watermark is None or watermark.last_complete_end >= window.start
        # An explicit --last range that does not touch the watermark must not
        # hide the windows between the watermark and this range.
        advance = not (forced and not contiguous)
        degraded, stored = (
            (None, {}) if force_replace or not rows else await self._recollection_degradation(window, rows)
        )
        if degraded is not None:
            # The stored rows of this window come from a better pass: keep them as they are.
            await self._record_skipped(window, rows, stored, degraded)
        else:
            records = self._flag_rejected(await self._window_records(window, rows), rejected)
            written, advanced = await self._write_window(
                window, rows, records, fence, advance=advance, force=force_replace
            )
            self._account_records(records, report)
            report.rows_written += written
            await self._note_opencost_seen(window)
            if advanced is not None:
                watermark = advanced
                advance = False
        report.windows_collected += 1
        if rows:
            # Dashboard gauges describe "now": keep only the most recent sample.
            latest = max((row.timestamp for row in rows if row.timestamp is not None), default=None)
            report.latest_rows = [row for row in rows if row.timestamp == latest]
        collection_telemetry.record_window(self.source, monotonic() - started)

        if not advance:
            return watermark
        # A window left as stored (degraded re-collection) still moves the watermark over its stored rows.
        fence.check("advance watermark")
        return await self.watermark_repository.advance(self.cluster_uid, self.source, window.end)

    # ------------------------------------------------------------------
    # Per-step record (COL-04)
    # ------------------------------------------------------------------

    async def _window_records(
        self, window: CollectionWindow, rows: list[CombinedMetric]
    ) -> list[CollectionWindowRecord]:
        """Classify every step of ``window``: collected, empty or failed (no scrape data).

        A step without any pod row is ``empty`` only when Prometheus holds
        cAdvisor scrape data for it (the kubelet was scraped, no pod ran);
        otherwise it is ``failed`` with reason ``no_scrape_data`` and is retried
        by later runs while it stays inside the backfill horizon.  The probe
        runs only when a step has no row.
        """
        if self.window_repository is None:
            return []
        pod_rows: dict[datetime, list[CombinedMetric]] = {}
        container_steps: set[datetime] = set()
        for row in rows:
            if row.timestamp is None:
                continue
            if getattr(row, "container_name", None):
                container_steps.add(ensure_utc(row.timestamp))
                continue
            pod_rows.setdefault(ensure_utc(row.timestamp), []).append(row)
        # Pod-level cAdvisor only (cri-dockerd): steps without container rows say why (COL-05).
        pod_level_only = getattr(self.processor, "last_container_series_present", None) is False
        reconciliation = getattr(self.processor, "last_cost_reconciliation", None)
        if not isinstance(reconciliation, dict):
            reconciliation = {}
        whole_window_pending = self._cost_pending()
        pending_steps = getattr(self.processor, "last_cost_pending_steps", None)
        if not isinstance(pending_steps, set):
            pending_steps = set()
        missing = [start for start in window.sample_starts() if start not in pod_rows]
        scraped: set[datetime] | None = None
        probe: Callable[[datetime, datetime], Awaitable[set[datetime]]] | None = getattr(
            self.processor, "probe_scrape_data", None
        )
        if missing and callable(probe):
            scraped = set(await probe(missing[0], missing[-1] + self.step))
        records: list[CollectionWindowRecord] = []
        for start in window.sample_starts():
            step_rows = pod_rows.get(start, [])
            opencost_total, attributed = reconciliation.get(start, (None, None))
            if step_rows:
                status = STATUS_COLLECTED
                reason = REASON_POD_LEVEL_ONLY if pod_level_only and start not in container_steps else None
            elif scraped is None:
                status, reason = STATUS_EMPTY, REASON_NOT_PROBED
            elif start in scraped:
                status, reason = STATUS_EMPTY, None
            else:
                status, reason = STATUS_FAILED, REASON_NO_SCRAPE_DATA
            records.append(
                CollectionWindowRecord(
                    cluster_uid=self.cluster_uid,
                    source=self.source,
                    window_start=start,
                    window_end=start + self.step,
                    status=status,
                    expected_steps=1,
                    steps_with_data=1 if step_rows else 0,
                    pod_rows=len(step_rows),
                    cpu_series=sum(1 for row in step_rows if row.cpu_usage_millicores is not None),
                    pods_without_cpu=sum(1 for row in step_rows if row.cpu_usage_millicores is None),
                    reason=reason,
                    opencost_total_cost=opencost_total,
                    attributed_cost=attributed,
                    cost_status=(
                        COST_STATUS_PENDING if step_rows and (whole_window_pending or start in pending_steps) else None
                    ),
                )
            )
        return records

    @staticmethod
    def _flag_rejected(records: list[CollectionWindowRecord], rejected: int) -> list[CollectionWindowRecord]:
        """Name the rejection on the collected steps of a window that lost rows (STO-03 step 3)."""
        if not rejected:
            return records
        return [
            replace(record, reason=REASON_ROWS_REJECTED)
            if record.status == STATUS_COLLECTED and record.reason is None
            else record
            for record in records
        ]

    def _account_records(self, records: list[CollectionWindowRecord], report: CollectionReport) -> None:
        """Telemetry and report counters of step records committed with their window."""
        if self.window_repository is None or not records:
            return
        by_status: dict[str, int] = {}
        for record in records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
        for status, count in by_status.items():
            collection_telemetry.record_steps(self.source, status, count)
        self._reconcile_cost(records, report)
        failed = by_status.get(STATUS_FAILED, 0)
        report.failed_steps += failed
        report.empty_steps += by_status.get(STATUS_EMPTY, 0)
        if failed:
            logger.error(
                "Prometheus has no scrape data for %d step(s) of %s -> %s; recorded as failed "
                "(no_scrape_data) and retried while inside COLLECTION_MAX_BACKFILL.",
                failed,
                records[0].window_start.isoformat(),
                records[-1].window_end.isoformat(),
            )

    def _reconcile_cost(self, records: list[CollectionWindowRecord], report: CollectionReport) -> None:
        """Share of the OpenCost window total that no stored row carries (COL-03 step 4)."""
        totals = [record for record in records if record.opencost_total_cost is not None]
        expected = sum(record.opencost_total_cost or 0.0 for record in totals)
        if expected <= 0:
            return
        attributed = sum(record.attributed_cost or 0.0 for record in totals)
        ratio = min(max((expected - attributed) / expected, 0.0), 1.0)
        report.cost_unattributed_ratio = ratio
        collection_telemetry.set_cost_unattributed_ratio(self.source, ratio)
        if ratio > _UNATTRIBUTED_COST_WARNING:
            logger.warning(
                "%.1f%% of the OpenCost cost of %s -> %s belongs to pods without any stored row.",
                ratio * 100,
                records[0].window_start.isoformat(),
                records[-1].window_end.isoformat(),
            )

    async def _record_gap(self, start: datetime, horizon_start: datetime, steps: int, fence: LeaderFence) -> None:
        """Record the steps abandoned before the backfill horizon as one gap range."""
        if self.window_repository is None or steps <= 0:
            return
        gap_start = align_down(start, self.step)
        if gap_start >= horizon_start:
            return
        fence.check("record collection gap")
        await self.window_repository.record(
            [
                CollectionWindowRecord(
                    cluster_uid=self.cluster_uid,
                    source=self.source,
                    window_start=gap_start,
                    window_end=horizon_start,
                    status=STATUS_GAP,
                    expected_steps=steps,
                    reason=REASON_BEYOND_BACKFILL,
                )
            ]
        )
        collection_telemetry.record_steps(self.source, STATUS_GAP, steps)

    async def _retry_failed_steps(self, now: datetime, fence: LeaderFence, report: CollectionReport) -> None:
        """Re-collect failed steps still inside the horizon; turn older ones into gaps.

        At most one chunk of steps is retried per run, least recently attempted
        first, so a long Prometheus hole never stalls the regular collection.
        """
        if self.window_repository is None:
            return
        horizon_start = align_down(now - self.max_backfill, self.step)
        expired = await self.window_repository.expire_failed(self.cluster_uid, self.source, horizon_start)
        if expired:
            logger.warning(
                "%d failed collection step(s) left COLLECTION_MAX_BACKFILL without data; recorded as gaps.", expired
            )
            collection_telemetry.record_steps(self.source, STATUS_GAP, expired)
        failed = await self.window_repository.list_failed(
            self.cluster_uid, self.source, horizon_start, self.max_chunk_steps
        )
        # Steps failed in this run are not retried at once.
        retry_before = report.planned_from
        starts = sorted(
            record.window_start for record in failed if retry_before is None or record.window_start < retry_before
        )
        for window in _contiguous_windows(starts, self.step):
            try:
                fence.check("retry failed window")
                rows, rejected = self._quarantine(await self.processor.run_range(start=window.start, end=window.end))
                degraded, stored = await self._recollection_degradation(window, rows) if rows else (None, {})
                if degraded is not None:
                    # A partial retry pass must not delete stored pods it does not see.
                    await self._record_skipped(window, rows, stored, degraded)
                    continue
                records = self._flag_rejected(await self._window_records(window, rows), rejected)
                fence.check("write retried window")
                written, _ = await self._write_window(window, rows, records, fence, advance=False)
                report.rows_written += written
                failed_before = report.failed_steps
                self._account_records(records, report)
                still_failed = report.failed_steps - failed_before
                report.failed_steps = failed_before
                report.recovered_steps += len(records) - still_failed
            except LeadershipLostError:
                raise
            except Exception as error:
                logger.warning(
                    "Retry of failed collection window %s -> %s failed: %s",
                    window.start.isoformat(),
                    window.end.isoformat(),
                    error,
                )
                collection_telemetry.record_window_error(self.source, "collection_failed")

    async def _note_opencost_seen(self, window: CollectionWindow) -> None:
        """Persist that OpenCost priced ``window`` for this cluster (COST-01 review 2)."""
        if getattr(self.processor, "last_cost_status", None) != "ok":
            return
        try:
            await self.watermark_repository.advance(self.cluster_uid, OPENCOST_SEEN_SOURCE, window.end)
        except Exception as error:  # a diagnostic fact: never fail the collection for it
            logger.warning("Could not record that OpenCost priced %s: %s", window.end.isoformat(), error)

    def _cost_pending(self) -> bool:
        """Whether the processor's last window waits for OpenCost (COST-01)."""
        return getattr(self.processor, "last_cost_pending", False) is True

    async def _seed_opencost_known(self, now: datetime) -> None:
        """Tell the processor OpenCost priced a stored step of the horizon (survives restarts, COST-01).

        An empty OpenCost answer is then a lag or a gap in OpenCost's data,
        and the window is marked pending instead of modeled for good.
        """
        if getattr(self.processor, "opencost_known", None) is not False:
            return
        seen = await self.watermark_repository.get(self.cluster_uid, OPENCOST_SEEN_SOURCE)
        if seen is not None:
            setattr(self.processor, "opencost_known", True)
            return
        if self.window_repository is None:
            return
        horizon_start = align_down(now - self.max_backfill, self.step)
        records = await self.window_repository.list_overlapping(self.cluster_uid, self.source, horizon_start, now)
        if any(record.opencost_total_cost is not None for record in records):
            setattr(self.processor, "opencost_known", True)

    async def _reprice_pending_steps(self, now: datetime, fence: LeaderFence, report: CollectionReport) -> None:
        """Re-collect steps whose cost waits for OpenCost, once OpenCost answers again (COST-01).

        A step recorded with ``cost_status = opencost_pending`` holds rows with
        a modeled cost labelled ``opencost_pending``. While the last window of
        this run was still pending, nothing is tried. Otherwise at most one
        chunk of pending steps inside the backfill horizon (least recently
        attempted first) is re-collected; the pass replaces the window when
        OpenCost priced it, which clears the status. A pass that would lose
        stored pods is not written and the steps stay pending (their rows stay
        untrusted). Steps that leave the horizon keep their pending rows.
        """
        if self.window_repository is None:
            return
        horizon_start = align_down(now - self.max_backfill, self.step)
        end = align_down(now - self.safety_lag, self.step)
        if end <= horizon_start:
            return
        expired = await self.window_repository.expire_cost_pending(self.cluster_uid, self.source, horizon_start)
        if expired:
            logger.warning(
                "%d collection step(s) left COLLECTION_MAX_BACKFILL while their cost still waited for OpenCost; "
                "their rows keep an untrusted opencost_pending cost (cost_status %s).",
                expired,
                COST_STATUS_PENDING_EXPIRED,
            )
            collection_telemetry.record_cost_pending_expired(self.source, expired)
        if not self._cost_pending():
            pending = await self.window_repository.list_cost_pending(
                self.cluster_uid, self.source, horizon_start, end, self.max_chunk_steps
            )
            for window in _contiguous_windows(sorted(record.window_start for record in pending), self.step):
                try:
                    fence.check("re-price pending window")
                    rows, rejected = self._quarantine(
                        await self.processor.run_range(start=window.start, end=window.end)
                    )
                    if self._cost_pending():
                        logger.warning(
                            "OpenCost still cannot price %s -> %s; the window stays opencost_pending.",
                            window.start.isoformat(),
                            window.end.isoformat(),
                        )
                        break
                    degraded, stored = await self._recollection_degradation(window, rows) if rows else (None, {})
                    if degraded is not None:
                        await self._record_skipped(window, rows, stored, degraded)
                        continue
                    records = self._flag_rejected(await self._window_records(window, rows), rejected)
                    fence.check("write re-priced window")
                    written, _ = await self._write_window(window, rows, records, fence, advance=False)
                    report.rows_written += written
                    # Only steps that hold rows and lost their pending status were re-priced.
                    report.repriced_steps += sum(
                        1 for record in records if record.cost_status is None and record.steps_with_data > 0
                    )
                    await self._note_opencost_seen(window)
                except LeadershipLostError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Re-pricing of window %s -> %s failed: %s",
                        window.start.isoformat(),
                        window.end.isoformat(),
                        error,
                    )
                    collection_telemetry.record_window_error(self.source, "collection_failed")
        remaining = await self.window_repository.list_cost_pending(
            self.cluster_uid, self.source, horizon_start, end, 1_000_000
        )
        report.cost_pending_steps = len(remaining)
        collection_telemetry.set_cost_pending_steps(self.source, len(remaining))
        if report.repriced_steps:
            logger.info("Re-priced %d collection step(s) from OpenCost after an outage.", report.repriced_steps)

    async def _seed_window_records(self, now: datetime) -> None:
        """Record the steps of rows stored before COL-04 as collected, once per cluster and source."""
        if self.window_repository is None:
            return
        if await self.window_repository.has_records(self.cluster_uid, self.source):
            return
        timestamps = await self.window_repository.stored_metric_timestamps()
        aligned = sorted({align_down(ts, self.step) for ts in timestamps if ts < now})
        if not aligned:
            return
        await self.window_repository.record(
            [
                CollectionWindowRecord(
                    cluster_uid=self.cluster_uid,
                    source=self.source,
                    window_start=start,
                    window_end=start + self.step,
                    status=STATUS_COLLECTED,
                    expected_steps=1,
                    steps_with_data=1,
                    reason=REASON_SEEDED,
                )
                for start in aligned
            ]
        )
        logger.info("Recorded %d stored collection step(s) in collection_windows (COL-04).", len(aligned))

    async def _check_scrape_interval(self, now: datetime, report: CollectionReport) -> None:
        """Hourly: does the rate window hold at least two scrapes of a cAdvisor series? (COL-04 step 3)."""
        measure: Callable[[datetime], Awaitable[float | None]] | None = getattr(
            self.processor, "measure_rate_window_scrapes", None
        )
        key = (self.cluster_uid, self.source)
        last_check = _SCRAPE_CHECKS.get(key)
        if not callable(measure) or (last_check is not None and now - last_check < _SCRAPE_CHECK_INTERVAL):
            return
        _SCRAPE_CHECKS[key] = now
        at = align_down(now - self.safety_lag, self.step)
        try:
            scrapes = await measure(at)
        except Exception as error:  # a diagnostic: never fails the collection
            logger.warning("Could not measure the scrapes per rate window: %s", error)
            return
        if scrapes is None:
            return
        report.rate_window_scrapes = scrapes
        collection_telemetry.set_rate_window_scrapes(self.source, scrapes)
        if scrapes < 2:
            logger.error(
                "The rate window holds %.0f scrape(s) of the cAdvisor series: rate() needs 2, so CPU usage is "
                "missing. Set PROMETHEUS_SCRAPE_INTERVAL (config.prometheus.scrapeInterval) to the real scrape "
                "interval.",
                scrapes,
            )

    async def _publish_coverage(self, now: datetime, report: CollectionReport) -> None:
        """Coverage of the backfill horizon, read from the table (it survives restarts)."""
        if self.window_repository is None:
            return
        end = align_down(now - self.safety_lag, self.step)
        start = align_down(now - self.max_backfill, self.step)
        if end <= start:
            return
        records = await self.window_repository.list_overlapping(self.cluster_uid, self.source, start, end)
        coverage = compute_coverage(records, start, end, self.step)
        report.coverage_ratio = coverage.ratio
        collection_telemetry.set_coverage_ratio(self.source, coverage.ratio)


def _contiguous_windows(starts: list[datetime], step: timedelta) -> list[CollectionWindow]:
    """Group sorted step starts into contiguous windows."""
    windows: list[CollectionWindow] = []
    run_start: datetime | None = None
    previous: datetime | None = None
    for start in starts:
        if run_start is not None and previous is not None and start != previous + step:
            windows.append(CollectionWindow(run_start, previous + step, step))
            run_start = None
        if run_start is None:
            run_start = start
        previous = start
    if run_start is not None and previous is not None:
        windows.append(CollectionWindow(run_start, previous + step, step))
    return windows
