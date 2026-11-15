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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Protocol

from ..collectors.promql import parse_duration_seconds
from ..models.metrics import CombinedMetric
from ..storage.base_collection_watermark_repository import CollectionWatermark, CollectionWatermarkRepository
from ..storage.base_collection_window_repository import (
    REASON_BEYOND_BACKFILL,
    REASON_NO_SCRAPE_DATA,
    REASON_NOT_PROBED,
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
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TARGET_CHUNK_SECONDS = 3600
#: Unattributed share of the OpenCost window total above which a warning is logged (COL-03 step 4).
_UNATTRIBUTED_COST_WARNING = 0.01


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
    async def write_combined_metrics(self, metrics: list[CombinedMetric], *, replace_containers: bool = False) -> int:
        """Upsert combined and container rows; ``replace_containers`` rewrites the container rows of each pod."""
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

    async def run(self, last: timedelta | None = None) -> CollectionReport:
        """Collect missing windows (or the explicit ``last`` range) oldest first."""
        report = CollectionReport()
        now = ensure_utc(self._clock())
        fence = self._fence_provider()
        watermark: CollectionWatermark | None = None
        try:
            watermark = await self.watermark_repository.get(self.cluster_uid, self.source)
            await self._seed_window_records(now)
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
                watermark = await self._collect_window(window, watermark, fence, report, forced=last is not None)
            if last is None:
                await self._retry_failed_steps(now, fence, report)
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

    async def _write_rows(self, rows: list[CombinedMetric]) -> int:
        """Write a collected window; its container rows replace the stored ones of its pods (review 4)."""
        return await self.metrics_repository.write_combined_metrics(rows, replace_containers=True)

    async def _write_idle_metrics(self) -> None:
        """Commits the ``__idle__`` rows of the window just collected (roadmap B2).

        Called after the fence check, like the pod rows. A failed write raises,
        so the watermark never advances over a window without its idle rows.
        """
        idle_rows = getattr(self.processor, "last_idle_metrics", None)
        write = getattr(self.metrics_repository, "write_idle_metrics", None)
        # Processors without idle rows (other range processors, test doubles) commit none.
        if not isinstance(idle_rows, (list, tuple)) or not idle_rows or write is None:
            return
        try:
            await write(list(idle_rows))
        except Exception as error:
            raise RuntimeError(f"Could not store {len(idle_rows)} node idle row(s): {error}") from error

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
    ) -> CollectionWatermark | None:
        started = monotonic()
        fence.check("collect metrics")
        rows = await self.processor.run_range(start=window.start, end=window.end)
        records = await self._window_records(window, rows)
        fence.check("write metrics")
        written = await self._write_rows(rows) if rows else 0
        await self._write_idle_metrics()
        await self._store_records(records, report)
        report.rows_written += written or 0
        report.windows_collected += 1
        if rows:
            # Dashboard gauges describe "now": keep only the most recent sample.
            latest = max((row.timestamp for row in rows if row.timestamp is not None), default=None)
            report.latest_rows = [row for row in rows if row.timestamp == latest]
        collection_telemetry.record_window(self.source, monotonic() - started)

        contiguous = watermark is None or watermark.last_complete_end >= window.start
        if forced and not contiguous:
            # An explicit --last range that does not touch the watermark must not
            # hide the windows between the watermark and this range.
            return watermark
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
        for row in rows:
            if row.timestamp is None or getattr(row, "container_name", None):
                continue
            pod_rows.setdefault(ensure_utc(row.timestamp), []).append(row)
        reconciliation = getattr(self.processor, "last_cost_reconciliation", None)
        if not isinstance(reconciliation, dict):
            reconciliation = {}
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
                status, reason = STATUS_COLLECTED, None
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
                )
            )
        return records

    async def _store_records(self, records: list[CollectionWindowRecord], report: CollectionReport) -> None:
        """Write step records after the rows, before the watermark moves (a failure fails the window)."""
        if self.window_repository is None or not records:
            return
        await self.window_repository.record(records)
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
                rows = await self.processor.run_range(start=window.start, end=window.end)
                records = await self._window_records(window, rows)
                fence.check("write retried window")
                if rows:
                    report.rows_written += await self._write_rows(rows) or 0
                    await self._write_idle_metrics()
                failed_before = report.failed_steps
                await self._store_records(records, report)
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
