"""Every collection step is recorded: collected, empty, failed or gap (COL-04)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core import collection_telemetry
from greenkube.core.collection import METRICS_WATERMARK_SOURCE, MetricsCollectionJob
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.storage.base_collection_window_repository import (
    REASON_BEYOND_BACKFILL,
    REASON_NO_SCRAPE_DATA,
    REASON_SEEDED,
    STATUS_COLLECTED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_GAP,
    CollectionWindowRecord,
    compute_coverage,
)
from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository
from greenkube.storage.sqlite.collection_window_repository import SQLiteCollectionWindowRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
CLUSTER = "test-cluster"


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def _available_from(threshold: datetime, value: float):
    """A series that exists only from ``threshold`` on (Prometheus lost everything before)."""
    cutoff = threshold.timestamp()
    return lambda ts: value if ts > cutoff else None


@pytest.fixture
async def stack(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "windows.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        COLLECTION_SAFETY_LAG="1m",
        COLLECTION_MAX_BACKFILL="24h",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
        CLUSTER_NAME=CLUSTER,
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    prom = FakePrometheus()
    metrics_repo = SQLiteCombinedMetricsRepository(dbm)
    watermarks = SQLiteCollectionWatermarkRepository(dbm)
    windows = SQLiteCollectionWindowRepository(dbm)
    processor = build_range_processor(
        cfg, metrics_repo, prom, FakeOpenCost(pods=[("ns", "p")]), pods=[("ns", "p", "app")]
    )
    clock = _Clock(BASE)

    def job() -> MetricsCollectionJob:
        return MetricsCollectionJob(
            processor=processor,
            metrics_repository=metrics_repo,
            watermark_repository=watermarks,
            config=cfg,
            clock=clock,
            window_repository=windows,
        )

    try:
        yield {"job": job, "clock": clock, "prom": prom, "windows": windows, "watermarks": watermarks}
    finally:
        await dbm.close()


async def _run(stack, at: datetime):
    stack["clock"].current = at
    return await stack["job"]().run()


async def _coverage(stack, start: datetime, end: datetime):
    records = await stack["windows"].list_overlapping(CLUSTER, METRICS_WATERMARK_SOURCE, start, end)
    return compute_coverage(records, start, end, STEP)


@pytest.mark.asyncio
async def test_downtime_beyond_the_horizon_records_gaps_and_failed_steps(stack):
    """COL-04 acceptance a) (S5): 72 h down, Prometheus holds the last 2 h only."""
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE)
    now = BASE + timedelta(hours=72, minutes=1, seconds=30)
    data_from = BASE + timedelta(hours=70)
    labels = {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}
    stack["prom"].add("container_cpu_usage_seconds_total", labels, _available_from(data_from, 0.1))

    report = await _run(stack, now)

    assert report.succeeded, report.error
    horizon = BASE + timedelta(hours=48)
    end = BASE + timedelta(hours=72)
    assert report.gap_windows == 48 * 12
    coverage = await _coverage(stack, BASE, end)
    assert coverage.expected_steps == 72 * 12
    assert coverage.gap_steps == 48 * 12
    assert coverage.collected_steps == 24
    assert coverage.failed_steps == 22 * 12
    assert coverage.empty_steps == 0 and coverage.unknown_steps == 0
    assert coverage.ratio == pytest.approx(24 / (72 * 12))
    assert not coverage.complete
    assert coverage.holes[0].status == STATUS_GAP and coverage.holes[0].reason == REASON_BEYOND_BACKFILL
    assert (coverage.holes[0].start, coverage.holes[0].end) == (BASE, horizon)
    assert coverage.holes[1].status == STATUS_FAILED and coverage.holes[1].reason == REASON_NO_SCRAPE_DATA
    assert (coverage.holes[1].start, coverage.holes[1].end) == (horizon, data_from)
    assert report.coverage_ratio == pytest.approx(24 / (24 * 12))
    gauge = collection_telemetry.COVERAGE_RATIO.labels(source=METRICS_WATERMARK_SOURCE)._value.get()
    assert gauge == pytest.approx(24 / (24 * 12))


@pytest.mark.asyncio
async def test_window_without_pods_while_cadvisor_is_scraped_is_empty(stack):
    """COL-04 acceptance b): Prometheus answers nothing for pods, but the kubelet was scraped."""
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE)
    # Root cgroup series: no pod, no container label.
    stack["prom"].add("container_cpu_usage_seconds_total", {"id": "/", "node": "n1"}, 0.3)

    report = await _run(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert report.succeeded, report.error
    assert report.empty_steps == 2 and report.failed_steps == 0
    coverage = await _coverage(stack, BASE, BASE + 2 * STEP)
    assert coverage.empty_steps == 2 and coverage.complete


@pytest.mark.asyncio
async def test_rate_window_with_fewer_than_two_scrapes_is_reported(stack, caplog):
    """COL-04 step 3: a scrape interval longer than half the rate window empties every rate()."""
    from greenkube.core import collection as collection_module

    collection_module._SCRAPE_CHECKS.clear()
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE)
    # The fake evaluates count_over_time as the raw value: one sample per rate window.
    stack["prom"].add("container_cpu_usage_seconds_total", {"id": "/", "node": "n1"}, 1.0)

    with caplog.at_level("ERROR"):
        report = await _run(stack, BASE + STEP + timedelta(minutes=1))
        again = await _run(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert report.rate_window_scrapes == 1.0
    assert again.rate_window_scrapes is None  # measured at most hourly
    assert collection_telemetry.RATE_WINDOW_SCRAPES.labels(source=METRICS_WATERMARK_SOURCE)._value.get() == 1.0
    assert any("PROMETHEUS_SCRAPE_INTERVAL" in record.message for record in caplog.records)
    collection_module._SCRAPE_CHECKS.clear()


@pytest.mark.asyncio
async def test_steps_of_a_pod_level_only_cluster_say_why_they_have_no_container_rows(stack):
    """COL-05 step 5: on cri-dockerd, catch-up windows have pod rows but no container rows."""
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE)
    stack["prom"].add("container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "node": "n1"}, 0.2)

    report = await _run(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert report.succeeded, report.error
    records = await stack["windows"].list_overlapping(CLUSTER, METRICS_WATERMARK_SOURCE, BASE, BASE + 2 * STEP)
    assert [(r.status, r.reason) for r in records] == [(STATUS_COLLECTED, "pod_level_usage_only")] * 2


@pytest.mark.asyncio
async def test_failed_step_is_retried_and_survives_a_restart(stack):
    """COL-04 acceptance e): the hole is in the table, and a later run fills it."""
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE)
    labels = {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}
    late = {"available": False}
    stack["prom"].add("container_cpu_usage_seconds_total", labels, lambda ts: 0.1 if late["available"] else None)

    first = await _run(stack, BASE + STEP + timedelta(minutes=1))

    assert first.succeeded and first.failed_steps == 1
    assert first.watermark == BASE + STEP  # a hole never stalls the collection
    hole = await stack["windows"].last_hole(CLUSTER, METRICS_WATERMARK_SOURCE)
    assert hole is not None and hole.status == STATUS_FAILED and hole.window_start == BASE

    # Remote write caught up: a new job object (controller restart) retries the step.
    late["available"] = True
    second = await _run(stack, BASE + 2 * STEP + timedelta(minutes=1))

    assert second.succeeded and second.recovered_steps == 1
    coverage = await _coverage(stack, BASE, BASE + 2 * STEP)
    assert coverage.collected_steps == 2 and coverage.complete


@pytest.mark.asyncio
async def test_failed_step_leaving_the_horizon_becomes_a_gap(stack):
    await stack["windows"].record(
        [
            CollectionWindowRecord(
                cluster_uid=CLUSTER,
                source=METRICS_WATERMARK_SOURCE,
                window_start=BASE,
                window_end=BASE + STEP,
                status=STATUS_FAILED,
                expected_steps=1,
                reason=REASON_NO_SCRAPE_DATA,
            )
        ]
    )
    now = BASE + timedelta(hours=25)
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, now - timedelta(minutes=4))

    report = await _run(stack, now)

    assert report.succeeded, report.error
    hole = await stack["windows"].last_hole(CLUSTER, METRICS_WATERMARK_SOURCE)
    assert hole is not None and hole.status == STATUS_GAP and hole.reason == REASON_NO_SCRAPE_DATA


@pytest.mark.asyncio
async def test_rows_stored_before_the_table_are_seeded_as_collected(stack, tmp_path):
    labels = {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}
    stack["prom"].add("container_cpu_usage_seconds_total", labels, 0.1)
    # Rows written by an earlier release, without window records.
    job = stack["job"]()
    job.window_repository = None
    stack["clock"].current = BASE + 2 * STEP + timedelta(minutes=1)
    await job.run(last=timedelta(minutes=10))
    await stack["watermarks"].advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE + 2 * STEP)

    await _run(stack, BASE + 2 * STEP + timedelta(minutes=2))

    records = await stack["windows"].list_overlapping(CLUSTER, METRICS_WATERMARK_SOURCE, BASE, BASE + 2 * STEP)
    assert [(r.window_start, r.status, r.reason) for r in records] == [
        (BASE, STATUS_COLLECTED, REASON_SEEDED),
        (BASE + STEP, STATUS_COLLECTED, REASON_SEEDED),
    ]


def test_single_step_record_overrides_a_gap_range_and_unknown_steps_are_reported():
    gap = CollectionWindowRecord(
        cluster_uid=CLUSTER,
        source=METRICS_WATERMARK_SOURCE,
        window_start=BASE,
        window_end=BASE + 4 * STEP,
        status=STATUS_GAP,
        expected_steps=4,
        reason=REASON_BEYOND_BACKFILL,
    )
    recollected = CollectionWindowRecord(
        cluster_uid=CLUSTER,
        source=METRICS_WATERMARK_SOURCE,
        window_start=BASE + STEP,
        window_end=BASE + 2 * STEP,
        status=STATUS_COLLECTED,
        expected_steps=1,
        steps_with_data=1,
    )
    empty = CollectionWindowRecord(
        cluster_uid=CLUSTER,
        source=METRICS_WATERMARK_SOURCE,
        window_start=BASE + 4 * STEP,
        window_end=BASE + 5 * STEP,
        status=STATUS_EMPTY,
        expected_steps=1,
    )

    coverage = compute_coverage([recollected, gap, empty], BASE + STEP, BASE + 6 * STEP, STEP)

    assert coverage.expected_steps == 5
    assert (coverage.collected_steps, coverage.gap_steps, coverage.empty_steps, coverage.unknown_steps) == (1, 2, 1, 1)
    assert coverage.ratio == pytest.approx(2 / 5)
    assert [(h.start, h.end, h.status) for h in coverage.holes] == [
        (BASE + 2 * STEP, BASE + 4 * STEP, STATUS_GAP),
        (BASE + 5 * STEP, BASE + 6 * STEP, "unknown"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("pods_in_prometheus", "expected_ratio"), [(("p", "q"), 0.0), (("p",), 0.5)])
async def test_opencost_cost_without_rows_is_reconciled(tmp_path, pods_in_prometheus, expected_ratio):
    """COL-03 step 4: the OpenCost window total is compared with the cost stored in rows."""
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "reconcile.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        COLLECTION_SAFETY_LAG="1m",
        COLLECTION_MAX_BACKFILL="24h",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
        CLUSTER_NAME=CLUSTER,
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    try:
        prom = FakePrometheus()
        for pod in pods_in_prometheus:
            prom.add(
                "container_cpu_usage_seconds_total",
                {"namespace": "ns", "pod": pod, "container": "app", "node": "n1"},
                0.1,
            )
        metrics_repo = SQLiteCombinedMetricsRepository(dbm)
        watermarks = SQLiteCollectionWatermarkRepository(dbm)
        windows = SQLiteCollectionWindowRepository(dbm)
        processor = build_range_processor(
            cfg, metrics_repo, prom, FakeOpenCost(pods=[("ns", "p"), ("ns", "q")]), pods=[("ns", "p", "app")]
        )
        start = BASE + STEP  # OpenCost cost of the window = minute / 100 = 0.05 per pod
        await watermarks.advance(CLUSTER, METRICS_WATERMARK_SOURCE, start)
        job = MetricsCollectionJob(
            processor=processor,
            metrics_repository=metrics_repo,
            watermark_repository=watermarks,
            config=cfg,
            clock=_Clock(start + STEP + timedelta(minutes=1)),
            window_repository=windows,
        )

        report = await job.run()

        assert report.succeeded, report.error
        (record,) = await windows.list_overlapping(CLUSTER, METRICS_WATERMARK_SOURCE, start, start + STEP)
        assert record.opencost_total_cost == pytest.approx(0.10)
        assert record.attributed_cost == pytest.approx(0.10 * (1 - expected_ratio))
        assert report.cost_unattributed_ratio == pytest.approx(expected_ratio)
        gauge = collection_telemetry.COST_UNATTRIBUTED_RATIO.labels(source=METRICS_WATERMARK_SOURCE)._value.get()
        assert gauge == pytest.approx(expected_ratio)
    finally:
        await dbm.close()
