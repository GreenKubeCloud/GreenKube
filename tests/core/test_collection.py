"""Half-open windows, watermarks and catch-up (roadmap A2, BUG-03) and leader fencing (A14, BUG-13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.core import collection_telemetry
from greenkube.core.collection import (
    METRICS_WATERMARK_SOURCE,
    CollectionWindow,
    MetricsCollectionJob,
    align_down,
    align_up,
    default_chunk_steps,
    plan_collection,
)
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.leader_election import LeaderFence
from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository
from greenkube.storage.sqlite.collection_window_repository import SQLiteCollectionWindowRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
PODS = (("ns", "p", "app"), ("ns", "q", "app"))


# ---------------------------------------------------------------------------
# CollectionWindow and planning
# ---------------------------------------------------------------------------


def test_collection_window_is_half_open_and_step_aligned():
    window = CollectionWindow(BASE, BASE + 3 * STEP, STEP)

    assert window.steps == 3
    assert window.contains(BASE)
    assert not window.contains(BASE + 3 * STEP)
    assert list(window.sample_starts()) == [BASE, BASE + STEP, BASE + 2 * STEP]
    assert window.first_evaluation == BASE + STEP
    assert window.last_evaluation == BASE + 3 * STEP
    assert [w.steps for w in window.split(2)] == [2, 1]
    assert window.split(2)[1].start == BASE + 2 * STEP


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (BASE + timedelta(minutes=1), BASE + STEP),  # unaligned start
        (BASE, BASE + timedelta(minutes=7)),  # partial step
        (BASE, BASE),  # empty
    ],
)
def test_collection_window_rejects_invalid_bounds(start, end):
    with pytest.raises(ValueError):
        CollectionWindow(start, end, STEP)


def test_covering_window_widens_to_whole_steps():
    window = CollectionWindow.covering(BASE + timedelta(minutes=2), BASE + timedelta(minutes=7), STEP)

    assert (window.start, window.end) == (BASE, BASE + 2 * STEP)
    assert align_down(BASE + timedelta(minutes=4, seconds=59), STEP) == BASE
    assert align_up(BASE + timedelta(seconds=1), STEP) == BASE + STEP
    assert align_up(BASE, STEP) == BASE


def test_plan_without_watermark_backfills_the_horizon_in_hourly_chunks():
    now = BASE + timedelta(minutes=7)
    plan = plan_collection(
        watermark=None,
        now=now,
        step=STEP,
        safety_lag=timedelta(minutes=1),
        max_backfill=timedelta(hours=24),
        max_chunk_steps=default_chunk_steps(STEP),
    )

    assert plan.windows[0].start == BASE + timedelta(minutes=5) - timedelta(hours=24)
    assert plan.windows[-1].end == BASE + STEP
    assert all(w.steps <= 12 for w in plan.windows)
    assert sum(w.steps for w in plan.windows) == 24 * 12
    assert plan.gap_windows == 0


def test_plan_from_recent_watermark_covers_only_complete_windows():
    plan = plan_collection(
        watermark=BASE,
        now=BASE + timedelta(minutes=10, seconds=30),
        step=STEP,
        safety_lag=timedelta(minutes=1),
        max_backfill=timedelta(hours=24),
        max_chunk_steps=12,
    )

    # [10:05, 10:10) closed at 10:10 but is only safe after 10:11.
    assert [(w.start, w.end) for w in plan.windows] == [(BASE, BASE + STEP)]


def test_plan_reports_windows_older_than_the_backfill_horizon_as_gaps():
    now = BASE + timedelta(hours=48, minutes=2)
    plan = plan_collection(
        watermark=BASE,
        now=now,
        step=STEP,
        safety_lag=timedelta(minutes=1),
        max_backfill=timedelta(hours=24),
        max_chunk_steps=12,
    )

    assert plan.horizon_start == BASE + timedelta(hours=24)
    assert plan.windows[0].start == BASE + timedelta(hours=24)
    assert plan.gap_windows == 24 * 12


def test_plan_is_empty_when_the_watermark_is_current():
    plan = plan_collection(
        watermark=BASE + STEP,
        now=BASE + STEP + timedelta(seconds=30),
        step=STEP,
        safety_lag=timedelta(minutes=1),
        max_backfill=timedelta(hours=24),
        max_chunk_steps=12,
    )

    assert plan.windows == ()


# ---------------------------------------------------------------------------
# End-to-end collection job with the real processor and SQLite
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
async def stack(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "collection.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        COLLECTION_SAFETY_LAG="1m",
        COLLECTION_MAX_BACKFILL="24h",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
        CLUSTER_NAME="test-cluster",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    prom = FakePrometheus()
    for namespace, pod, container in PODS:
        prom.add(
            "container_cpu_usage_seconds_total",
            {"namespace": namespace, "pod": pod, "container": container, "node": "n1"},
            0.1,
        )
        # Pod cgroup series must never be double counted (A1).
        prom.add("container_cpu_usage_seconds_total", {"namespace": namespace, "pod": pod, "node": "n1"}, 0.1)
    opencost = FakeOpenCost(pods=[(ns, pod) for ns, pod, _ in PODS])
    metrics_repo = SQLiteCombinedMetricsRepository(dbm)
    watermarks = SQLiteCollectionWatermarkRepository(dbm)
    processor = build_range_processor(cfg, metrics_repo, prom, opencost, pods=PODS)
    clock = _Clock(BASE)

    def job(**kwargs) -> MetricsCollectionJob:
        return MetricsCollectionJob(
            processor=processor,
            metrics_repository=metrics_repo,
            watermark_repository=watermarks,
            config=cfg,
            clock=clock,
            **kwargs,
        )

    try:
        yield {
            "job": job,
            "clock": clock,
            "prom": prom,
            "opencost": opencost,
            "metrics_repo": metrics_repo,
            "watermarks": watermarks,
            "processor": processor,
        }
    finally:
        await dbm.close()


async def _pod_rows(metrics_repo, start: datetime, end: datetime):
    rows = await metrics_repo.read_combined_metrics(start, end)
    return sorted((r for r in rows if not r.container_name), key=lambda r: (r.timestamp, r.pod_name))


async def _run_scheduled(stack, run_at: datetime):
    stack["clock"].current = run_at
    return await stack["job"]().run()


@pytest.mark.asyncio
async def test_two_hours_of_scheduled_runs_produce_24_windows_and_rows_per_pod(stack):
    """A2 acceptance: 2 h at a 5-minute step = 24 windows and 24 × pods rows, no gaps."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)

    collected = 0
    for k in range(1, 25):
        # The scheduler fires on the step boundary + safety lag (+ a few seconds of drift).
        report = await _run_scheduled(stack, BASE + k * STEP + timedelta(minutes=1, seconds=3))
        assert report.succeeded, report.error
        collected += report.windows_collected

    assert collected == 24
    assert len(stack["opencost"].calls) == 24
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + timedelta(hours=2))
    assert len(rows) == 24 * len(PODS)
    expected = [BASE + k * STEP for k in range(24)]
    for _ns, pod, _container in PODS:
        assert [r.timestamp for r in rows if r.pod_name == pod] == expected
    assert all(r.cpu_usage_millicores == 100 for r in rows)
    watermark = await stack["watermarks"].get("test-cluster", METRICS_WATERMARK_SOURCE)
    assert watermark is not None and watermark.last_complete_end == BASE + timedelta(hours=2)


@pytest.mark.asyncio
async def test_restart_after_30_minutes_of_downtime_backfills_the_gap(stack):
    """A2 acceptance: killing the controller for 30 minutes leaves no permanent gap."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    for k in (1, 2):
        await _run_scheduled(stack, BASE + k * STEP + timedelta(minutes=1))

    # Controller down from 10:11 to 10:41; first run after restart catches up.
    report = await _run_scheduled(stack, BASE + timedelta(minutes=41, seconds=20))

    assert report.succeeded
    assert report.gap_windows == 0
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + timedelta(hours=1))
    assert [r.timestamp for r in rows if r.pod_name == "p"] == [BASE + k * STEP for k in range(8)]
    assert report.watermark == BASE + 8 * STEP


@pytest.mark.asyncio
async def test_rerunning_a_window_is_an_idempotent_upsert(stack):
    """A2 acceptance: re-collecting the same window keeps one row with the same values."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))
    first = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)

    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    report = await stack["job"]().run(last=timedelta(minutes=10))
    second = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)

    assert report.succeeded and report.windows_collected == 1
    assert [(r.timestamp, r.pod_name, r.joules, r.total_cost) for r in second] == [
        (r.timestamp, r.pod_name, r.joules, r.total_cost) for r in first
    ]


@pytest.mark.asyncio
async def test_prometheus_outage_does_not_advance_the_watermark(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    stack["prom"].fail_queries_containing = ("container_cpu_usage_seconds_total",)

    failed = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))

    assert not failed.succeeded
    assert failed.watermark == BASE
    assert await _pod_rows(stack["metrics_repo"], BASE, BASE + STEP) == []

    stack["prom"].fail_queries_containing = ()
    recovered = await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert recovered.succeeded and recovered.windows_collected == 1
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert [r.timestamp for r in rows if r.pod_name == "p"] == [BASE, BASE + STEP]


@pytest.mark.asyncio
async def test_failed_pod_list_does_not_advance_the_watermark(stack):
    """COL-07 acceptance (S4): a raising pod list fails the window; the next tick stores requests."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    pod_collector = stack["processor"].pod_collector
    working = pod_collector.collect
    pod_collector.collect = AsyncMock(side_effect=RuntimeError("API server timeout"))

    failed = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))

    assert not failed.succeeded and "pod list" in (failed.error or "")
    assert failed.watermark == BASE
    assert await _pod_rows(stack["metrics_repo"], BASE, BASE + STEP) == []

    pod_collector.collect = working
    recovered = await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert recovered.succeeded and recovered.windows_collected == 1
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert rows and all(r.cpu_request == 1000 for r in rows)


@pytest.mark.asyncio
async def test_watermark_older_than_horizon_counts_gap_windows(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    before = collection_telemetry.GAP_WINDOWS.labels(source=METRICS_WATERMARK_SOURCE)._value.get()

    report = await _run_scheduled(stack, BASE + timedelta(hours=24, minutes=11))

    assert report.succeeded
    assert report.gap_windows == 2
    assert collection_telemetry.GAP_WINDOWS.labels(source=METRICS_WATERMARK_SOURCE)._value.get() == before + 2
    lag = collection_telemetry.WATERMARK_LAG.labels(source=METRICS_WATERMARK_SOURCE)._value.get()
    assert lag == pytest.approx(timedelta(minutes=1).total_seconds())


@pytest.mark.asyncio
async def test_explicit_last_range_does_not_jump_over_an_older_watermark(stack):
    """--last 10min far ahead of the watermark must not hide the windows in between."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    stack["clock"].current = BASE + timedelta(hours=2, minutes=1)

    report = await stack["job"]().run(last=timedelta(minutes=10))

    assert report.succeeded and report.windows_collected == 1
    assert report.watermark == BASE


# ---------------------------------------------------------------------------
# Leader fencing (A14)
# ---------------------------------------------------------------------------


class _Elector:
    def __init__(self) -> None:
        self.is_leader = True
        self.token: int | None = 3

    @property
    def fencing_token(self) -> int | None:
        return self.token if self.is_leader else None


@pytest.mark.asyncio
async def test_lost_lease_mid_job_aborts_before_writing_or_advancing(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    elector = _Elector()
    fence = LeaderFence(elector)  # type: ignore[arg-type]
    processor = stack["job"]().processor
    original = processor.run_range

    async def run_range_then_lose_lease(*args, **kwargs):
        rows = await original(*args, **kwargs)
        elector.is_leader = False  # Lease lost while Prometheus was being queried
        return rows

    processor.run_range = run_range_then_lose_lease
    try:
        stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
        report = await stack["job"](fence_provider=lambda: fence).run()
    finally:
        processor.run_range = original

    assert not report.succeeded and "Lease" in (report.error or "")
    assert await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP) == []
    # The __idle__ rows are a commit too: a fenced-out replica writes none (B2, A14).
    assert await stack["metrics_repo"].read_idle_metrics(BASE, BASE + 2 * STEP) == []
    watermark = await stack["watermarks"].get("test-cluster", METRICS_WATERMARK_SOURCE)
    assert watermark is not None and watermark.last_complete_end == BASE


@pytest.mark.asyncio
async def test_collected_windows_store_idle_rows_next_to_pod_rows(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)

    report = await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert report.succeeded, report.error
    idle = await stack["metrics_repo"].read_idle_metrics(BASE, BASE + 2 * STEP)
    assert sorted(row.timestamp for row in idle) == [BASE, BASE + STEP]
    assert {row.node for row in idle} == {"n1"}


@pytest.mark.asyncio
async def test_failed_idle_write_does_not_advance_the_watermark(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    metrics_repo = stack["metrics_repo"]
    original = metrics_repo.write_idle_metrics

    async def failing_write(_rows, **_kwargs):
        raise RuntimeError("disk full")

    metrics_repo.write_idle_metrics = failing_write
    try:
        report = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))
    finally:
        metrics_repo.write_idle_metrics = original

    assert not report.succeeded and "idle" in (report.error or "")
    assert report.watermark == BASE
    # STO-03: the window is atomic, so the failed idle write leaves no pod or container row behind.
    assert await metrics_repo.read_combined_metrics(BASE, BASE + STEP) == []
    assert await metrics_repo.read_container_metrics(BASE, BASE + STEP) == []

    recovered = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))
    assert recovered.succeeded and recovered.watermark == BASE + STEP
    assert len(await _pod_rows(metrics_repo, BASE, BASE + STEP)) == len(PODS)
    assert len(await metrics_repo.read_idle_metrics(BASE, BASE + STEP)) == 1


@pytest.mark.asyncio
async def test_a_row_the_database_rejects_fails_the_window_without_storing_any_row(stack):
    """STO-04 (SIM A1): one out-of-range value rolls back the whole window instead of being logged away."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    processor = stack["processor"]
    collect = processor.run_range

    async def with_bad_row(*args, **kwargs):
        rows = await collect(*args, **kwargs)
        pod_rows = [row for row in rows if not row.container_name]
        pod_rows[0].restart_count = 2**64
        return rows

    processor.run_range = with_bad_row
    try:
        failed = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))
    finally:
        processor.run_range = collect

    assert not failed.succeeded and failed.watermark == BASE
    assert await stack["metrics_repo"].read_combined_metrics(BASE, BASE + STEP) == []
    recovered = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))
    assert recovered.succeeded and len(await _pod_rows(stack["metrics_repo"], BASE, BASE + STEP)) == len(PODS)


@pytest.mark.asyncio
async def test_a_row_without_timestamp_is_rejected_and_counted_and_the_window_logged_partial(stack):
    """STO-03 step 3: invalid rows are quarantined; the other rows of the window are stored."""
    window_repository = SQLiteCollectionWindowRepository(stack["metrics_repo"].db_manager)
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    processor = stack["processor"]
    collect = processor.run_range

    async def with_unplaced_container_row(*args, **kwargs):
        rows = await collect(*args, **kwargs)
        container = next(row for row in rows if row.container_name)
        return [*rows, container.model_copy(update={"timestamp": None, "container_name": "ghost"})]

    rejected = collection_telemetry.ROWS_REJECTED.labels(source=METRICS_WATERMARK_SOURCE, reason="no_timestamp")
    before = rejected._value.get()
    processor.run_range = with_unplaced_container_row
    try:
        stack["clock"].current = BASE + STEP + timedelta(minutes=1)
        report = await stack["job"](window_repository=window_repository).run()
    finally:
        processor.run_range = collect

    assert report.succeeded, report.error
    assert report.watermark == BASE + STEP
    assert rejected._value.get() == before + 1
    assert len(await _pod_rows(stack["metrics_repo"], BASE, BASE + STEP)) == len(PODS)
    assert "ghost" not in {
        c.container_name for c in await stack["metrics_repo"].read_container_metrics(BASE, BASE + STEP)
    }
    records = await window_repository.list_overlapping("test-cluster", METRICS_WATERMARK_SOURCE, BASE, BASE + STEP)
    assert [(r.status, r.reason) for r in records] == [("collected", "rows_rejected")]


@pytest.mark.asyncio
async def test_lease_taken_over_and_regained_still_aborts(stack):
    """A changed Lease generation means another controller may have written in between."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    elector = _Elector()
    fence = LeaderFence(elector)  # type: ignore[arg-type]
    elector.token = 5  # another holder took the Lease, then this process re-acquired it

    stack["clock"].current = BASE + STEP + timedelta(minutes=1)
    report = await stack["job"](fence_provider=lambda: fence).run()

    assert not report.succeeded
    watermark = await stack["watermarks"].get("test-cluster", METRICS_WATERMARK_SOURCE)
    assert watermark is not None and watermark.last_complete_end == BASE


@pytest.mark.asyncio
async def test_report_keeps_only_the_latest_sample_for_dashboard_gauges(stack):
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)

    report = await _run_scheduled(stack, BASE + 3 * STEP + timedelta(minutes=1))

    assert report.windows_collected == 1
    assert {row.timestamp for row in report.latest_rows} == {BASE + 2 * STEP}
    assert {row.pod_name for row in report.latest_rows if not row.container_name} == {"p", "q"}


@pytest.mark.asyncio
async def test_intensity_lookup_error_does_not_advance_the_watermark(stack):
    """COL-08 acceptance (S10): rows are not dropped silently; the window is retried."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    calculator = stack["processor"].calculator
    working = calculator.calculate_emissions
    calculator.calculate_emissions = AsyncMock(side_effect=RuntimeError("intensity table unavailable"))

    failed = await _run_scheduled(stack, BASE + STEP + timedelta(minutes=1))

    assert not failed.succeeded and "intensity" in (failed.error or "")
    assert failed.watermark == BASE

    calculator.calculate_emissions = working
    recovered = await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert recovered.succeeded
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert [r.timestamp for r in rows if r.pod_name == "p"] == [BASE, BASE + STEP]


async def _generations(stack, start: datetime, end: datetime) -> set:
    connection = stack["metrics_repo"].db_manager.connection
    found = set()
    for table in ("combined_metrics", "container_metrics", "node_idle_metrics"):
        async with connection.execute(
            f'SELECT DISTINCT collection_run_id, collected_at IS NOT NULL FROM {table} WHERE "timestamp" >= ? '
            'AND "timestamp" < ?',
            (start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")),
        ) as cursor:
            found.update(tuple(row) for row in await cursor.fetchall())
    return found


def _skipped(reason: str) -> float:
    return collection_telemetry.RECOLLECT_SKIPPED.labels(source=METRICS_WATERMARK_SOURCE, reason=reason)._value.get()


@pytest.mark.asyncio
async def test_recollected_window_holds_one_generation_with_the_same_totals(stack):
    """STO-01 c): after a re-collection every pod, container and idle row of W carries the same run id."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))
    first = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    first_idle = await stack["metrics_repo"].read_idle_metrics(BASE, BASE + 2 * STEP)
    before = await _generations(stack, BASE, BASE + 2 * STEP)

    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    report = await stack["job"]().run(last=timedelta(minutes=10))

    assert report.succeeded and report.rows_written > 0
    after = await _generations(stack, BASE, BASE + 2 * STEP)
    assert len(after) == 1 and after != before
    (run_id, stamped) = next(iter(after))
    assert run_id and stamped
    second = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    second_idle = await stack["metrics_repo"].read_idle_metrics(BASE, BASE + 2 * STEP)
    total = sum(r.joules for r in first) + sum(r.joules for r in first_idle)
    assert sum(r.joules for r in second) + sum(r.joules for r in second_idle) == pytest.approx(total, rel=1e-9)


@pytest.mark.asyncio
async def test_recollection_without_opencost_leaves_the_stored_window(stack):
    """STO-01 a): a re-collection while OpenCost has no data keeps the OpenCost rows and counts the skip."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))
    first = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert {r.cost_source for r in first} == {"opencost"}
    skipped = _skipped("cost_unavailable")

    stack["opencost"].pods = []
    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    report = await stack["job"]().run(last=timedelta(minutes=10))

    assert report.succeeded and report.rows_written == 0
    second = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert [(r.pod_name, r.cost_source, r.total_cost, r.cpu_request) for r in second] == [
        (r.pod_name, r.cost_source, r.total_cost, r.cpu_request) for r in first
    ]
    assert _skipped("cost_unavailable") == skipped + 1


def _windows(stack) -> SQLiteCollectionWindowRepository:
    return SQLiteCollectionWindowRepository(stack["metrics_repo"].db_manager)


@pytest.mark.asyncio
async def test_skipped_recollection_is_recorded_and_force_replaces_the_window(stack):
    """Review 2: a skipped re-collection is a step record; --last --force replaces the stored rows anyway."""
    windows = _windows(stack)
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    await stack["job"](window_repository=windows).run()

    stack["opencost"].pods = []
    skipped = await stack["job"](window_repository=windows).run(last=timedelta(minutes=10))
    assert skipped.succeeded and skipped.rows_written == 0
    records = await windows.list_overlapping("test-cluster", METRICS_WATERMARK_SOURCE, BASE, BASE + 2 * STEP)
    assert {(r.status, r.reason, r.pod_rows) for r in records} == {("collected", "recollect_skipped", len(PODS))}

    forced = await stack["job"](window_repository=windows).run(last=timedelta(minutes=10), force=True)
    assert forced.succeeded and forced.rows_written > 0
    rows = await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)
    assert rows and {r.cost_source for r in rows} != {"opencost"}
    with pytest.raises(ValueError):
        await stack["job"]().run(force=True)


@pytest.mark.asyncio
async def test_recollection_missing_a_stored_pod_is_skipped_even_with_the_same_pod_count(stack):
    """Review 4: pod sets are compared, not counts."""
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    await _run_scheduled(stack, BASE + 2 * STEP + timedelta(minutes=1))
    processor = stack["processor"]
    collect = processor.run_range

    async def renamed(*args, **kwargs):
        return [
            row.model_copy(update={"pod_name": "r"}) if row.pod_name == "q" else row
            for row in await collect(*args, **kwargs)
        ]

    skipped = _skipped("missing_pods")
    processor.run_range = renamed
    try:
        stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
        report = await stack["job"]().run(last=timedelta(minutes=10))
    finally:
        processor.run_range = collect
    assert report.succeeded and report.rows_written == 0
    assert {r.pod_name for r in await _pod_rows(stack["metrics_repo"], BASE, BASE + 2 * STEP)} == {"p", "q"}
    assert _skipped("missing_pods") == skipped + 1


@pytest.mark.asyncio
async def test_retry_of_a_failed_step_never_deletes_stored_pods_a_partial_pass_misses(stack):
    """Review 3: the retry path applies the same degradation check as a re-collection."""
    windows = _windows(stack)
    await stack["watermarks"].advance("test-cluster", METRICS_WATERMARK_SOURCE, BASE)
    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    await stack["job"](window_repository=windows).run()
    # The first step is recorded as failed although it holds rows (for example a record lost in a crash).
    await windows.db_manager.connection.execute(
        "UPDATE collection_windows SET status = 'failed', reason = 'no_scrape_data' WHERE window_start = ?",
        (BASE.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
    )
    await windows.db_manager.connection.commit()
    processor = stack["processor"]
    collect = processor.run_range

    async def without_q(*args, **kwargs):
        return [row for row in await collect(*args, **kwargs) if row.pod_name != "q"]

    processor.run_range = without_q
    try:
        stack["clock"].current = BASE + 3 * STEP + timedelta(minutes=1)
        report = await stack["job"](window_repository=windows).run()
    finally:
        processor.run_range = collect
    assert report.succeeded, report.error
    assert {r.pod_name for r in await _pod_rows(stack["metrics_repo"], BASE, BASE + STEP)} == {"p", "q"}
    (record,) = await windows.list_overlapping("test-cluster", METRICS_WATERMARK_SOURCE, BASE, BASE + STEP)
    assert record.reason == "recollect_skipped"
