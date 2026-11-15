"""An OpenCost outage never replaces OpenCost cost silently; the window is re-priced later (COST-01, RECON-09)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from greenkube.collectors.opencost_collector import (
    OPENCOST_STATUS_EMPTY,
    OPENCOST_STATUS_NOT_CONFIGURED,
    OPENCOST_STATUS_OK,
    OpenCostCollector,
    OpenCostUnavailableError,
)
from greenkube.core.collection import METRICS_WATERMARK_SOURCE, MetricsCollectionJob
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import CostMetric
from greenkube.models.provenance import CostSource, is_cost_trusted
from greenkube.storage.base_collection_window_repository import COST_STATUS_PENDING
from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository
from greenkube.storage.sqlite.collection_window_repository import SQLiteCollectionWindowRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import build_range_processor  # pyrefly: ignore[missing-import]

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
CLUSTER = "test-cluster"


# ---------------------------------------------------------------------------
# Collector contract (acceptance f)
# ---------------------------------------------------------------------------


def _collector(handler) -> OpenCostCollector:
    collector = OpenCostCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector._resolved_url = "http://opencost/allocation/compute"
    return collector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="unavailable"),
        httpx.Response(401, text="unauthorized"),
        httpx.Response(200, text="<html>not json</html>"),
    ],
    ids=["503", "401", "invalid-json"],
)
async def test_failed_allocation_request_raises_instead_of_returning_nothing(response):
    collector = _collector(lambda request: response)
    try:
        with pytest.raises(OpenCostUnavailableError):
            await collector.collect_range(BASE, BASE + STEP)
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_empty_and_successful_answers_report_their_status():
    payloads = iter(
        [
            {"code": 200, "data": [{}]},
            {
                "code": 200,
                "data": [
                    {
                        "ns/p": {
                            "properties": {"namespace": "ns", "pod": "p"},
                            "minutes": 5,
                            "cpuCost": 0.01,
                            "ramCost": 0.002,
                            "totalCost": 0.012,
                        }
                    }
                ],
            },
        ]
    )
    collector = _collector(lambda request: httpx.Response(200, json=next(payloads)))
    try:
        assert await collector.collect_range(BASE, BASE + STEP) == []
        assert collector.last_status == OPENCOST_STATUS_EMPTY
        (metric,) = await collector.collect_range(BASE, BASE + STEP)
        assert metric.total_cost == pytest.approx(0.012)
        assert collector.last_status == OPENCOST_STATUS_OK
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_without_any_opencost_the_status_is_not_configured():
    collector = OpenCostCollector()
    with patch.object(collector, "_resolve_url", AsyncMock(return_value=None)):
        assert await collector.collect_range(BASE, BASE + STEP) == []
    assert collector.last_status == OPENCOST_STATUS_NOT_CONFIGURED
    await collector.close()


# ---------------------------------------------------------------------------
# Collection job: pending provenance, step record and automatic re-pricing
# ---------------------------------------------------------------------------


class _ToggleOpenCost:
    """OpenCost whose window cost is ``minute / 100``; ``down`` makes it unavailable, ``absent`` unconfigured."""

    def __init__(self) -> None:
        self.down = False
        self.absent = False
        self.last_status: str | None = None

    async def collect_range(self, start, end):
        if self.absent:
            self.last_status = OPENCOST_STATUS_NOT_CONFIGURED
            return []
        if self.down:
            self.last_status = None
            raise OpenCostUnavailableError("HTTP 503 from OpenCost")
        self.last_status = OPENCOST_STATUS_OK
        return [
            CostMetric(
                pod_name="p",
                namespace="ns",
                cpu_cost=start.minute / 100.0,
                ram_cost=0.0,
                total_cost=start.minute / 100.0,
                window_start=start,
                window_end=end,
                minutes=(end - start).total_seconds() / 60,
            )
        ]


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
async def stack(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "outage.db"),
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
    prom.add(
        "container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}, 0.1
    )
    metrics_repo = SQLiteCombinedMetricsRepository(dbm)
    watermarks = SQLiteCollectionWatermarkRepository(dbm)
    windows = SQLiteCollectionWindowRepository(dbm)
    opencost = _ToggleOpenCost()
    processor = build_range_processor(cfg, metrics_repo, prom, opencost, pods=[("ns", "p", "app")])
    clock = _Clock(BASE)
    await watermarks.advance(CLUSTER, METRICS_WATERMARK_SOURCE, BASE + STEP)

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
        yield {"job": job, "clock": clock, "opencost": opencost, "windows": windows, "metrics": metrics_repo}
    finally:
        await dbm.close()


async def _collect_until(stack, window_end: datetime, **kwargs):
    stack["clock"].current = window_end + timedelta(minutes=1)
    report = await stack["job"]().run(**kwargs)
    assert report.succeeded, report.error
    return report


async def _pod_row(stack, start: datetime):
    rows = await stack["metrics"].read_combined_metrics(start, start + STEP)
    (row,) = [row for row in rows if row.pod_name == "p"]
    return row


async def _record(stack, start: datetime):
    (record,) = await stack["windows"].list_overlapping(CLUSTER, METRICS_WATERMARK_SOURCE, start, start + STEP)
    return record


@pytest.mark.asyncio
async def test_outage_window_is_pending_untrusted_and_repriced_when_opencost_is_back(stack):
    w1, w2, w3 = BASE + STEP, BASE + 2 * STEP, BASE + 3 * STEP
    await _collect_until(stack, w1 + STEP)
    assert (await _pod_row(stack, w1)).cost_source == CostSource.OPENCOST.value

    # (a) OpenCost answers 503 for window 2: usage rows are written, cost pending, watermark advances.
    stack["opencost"].down = True
    report = await _collect_until(stack, w2 + STEP)
    assert report.watermark == w2 + STEP
    pending = await _pod_row(stack, w2)
    assert pending.cost_source == CostSource.OPENCOST_PENDING.value
    assert pending.total_cost and pending.total_cost > 0
    assert not is_cost_trusted(pending)
    assert (await _record(stack, w2)).cost_status == COST_STATUS_PENDING

    # (c) A forced re-collection of window 1 while OpenCost is down keeps its stored OpenCost cost.
    await _collect_until(stack, w2 + STEP, last=timedelta(minutes=11))
    kept = await _pod_row(stack, w1)
    assert kept.cost_source == CostSource.OPENCOST.value
    assert kept.total_cost == pytest.approx(0.05)

    # (b) OpenCost is back: the next run re-prices window 2 and clears its pending record.
    stack["opencost"].down = False
    await _collect_until(stack, w3 + STEP)
    repriced = await _pod_row(stack, w2)
    assert repriced.cost_source == CostSource.OPENCOST.value
    assert repriced.total_cost == pytest.approx(0.10)
    assert (await _record(stack, w2)).cost_status is None
    assert (await _pod_row(stack, w3)).total_cost == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_cluster_without_opencost_stores_modeled_rows_and_nothing_pending(stack):
    """(e) A cluster that never had OpenCost: modeled rows, no pending record."""
    stack["opencost"].absent = True
    w1 = BASE + STEP
    await _collect_until(stack, w1 + STEP)
    row = await _pod_row(stack, w1)
    assert row.cost_source in {CostSource.PRICING_CATALOG.value, CostSource.CUSTOM_RATE.value}
    assert (await _record(stack, w1)).cost_status is None


@pytest.mark.asyncio
async def test_empty_answer_after_opencost_had_data_is_pending(stack):
    w1, w2 = BASE + STEP, BASE + 2 * STEP
    await _collect_until(stack, w1 + STEP)

    async def empty(start, end):
        stack["opencost"].last_status = OPENCOST_STATUS_EMPTY
        return []

    stack["opencost"].collect_range = empty
    await _collect_until(stack, w2 + STEP)
    assert (await _pod_row(stack, w2)).cost_source == CostSource.OPENCOST_PENDING.value
    assert (await _record(stack, w2)).cost_status == COST_STATUS_PENDING
