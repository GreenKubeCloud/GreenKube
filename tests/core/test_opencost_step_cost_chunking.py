"""Per-step OpenCost cost does not depend on how the window was chunked (COL-10, RECON-08)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.opencost_collector import _allocation_to_cost_metric, opencost_step_shares
from greenkube.core.config import Config
from greenkube.models.metrics import CostMetric
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import build_range_processor  # pyrefly: ignore[missing-import]

STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_allocation_keeps_the_running_interval_of_the_pod():
    item = {
        "properties": {"namespace": "batch", "pod": "job-7min"},
        "window": {"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=1))},
        "start": _iso(BASE + timedelta(minutes=2)),
        "end": _iso(BASE + timedelta(minutes=9)),
        "minutes": 7,
        "cpuCost": 0.7,
        "ramCost": 0.0,
        "totalCost": 0.7,
    }
    metric = _allocation_to_cost_metric("batch/job-7min", item, BASE)
    assert metric is not None
    assert metric.running_start == BASE + timedelta(minutes=2)
    assert metric.running_end == BASE + timedelta(minutes=9)


def test_step_shares_follow_running_seconds_in_each_step():
    """S9: job-7min ran 3 min in step 1 and 4 min in step 2: 0.30 and 0.40 of 0.70."""
    cost = CostMetric(
        pod_name="job-7min",
        namespace="batch",
        cpu_cost=0.7,
        ram_cost=0.0,
        total_cost=0.7,
        window_start=BASE,
        window_end=BASE + timedelta(hours=1),
        running_start=BASE + timedelta(minutes=2),
        running_end=BASE + timedelta(minutes=9),
        minutes=7,
    )
    shares = opencost_step_shares(cost, [BASE + i * STEP for i in range(12)], STEP)
    assert shares[BASE] * 0.7 == pytest.approx(0.30, rel=0.01)
    assert shares[BASE + STEP] * 0.7 == pytest.approx(0.40, rel=0.01)
    assert sum(shares.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("start_minute", [0, 1, 4, 13, 29, 55, 59])
@pytest.mark.parametrize("length", [1, 3, 7, 12, 31, 60])
@pytest.mark.parametrize("rows", [{0}, {1, 2}, {5, 6, 7, 11}, set(range(12))], ids=["r0", "r12", "r5-11", "all"])
def test_shares_of_a_pod_sum_to_the_allocation(start_minute, length, rows):
    """Property: the per-pod cost spread over its rows equals the allocation when every running step has a row."""
    end_minute = min(start_minute + length, 60)
    cost = CostMetric(
        pod_name="p",
        namespace="n",
        cpu_cost=1.0,
        ram_cost=0.0,
        total_cost=1.0,
        window_start=BASE,
        window_end=BASE + timedelta(hours=1),
        running_start=BASE + timedelta(minutes=start_minute),
        running_end=BASE + timedelta(minutes=end_minute),
        minutes=end_minute - start_minute,
    )
    running_steps = {i for i in range(12) if i * 5 < end_minute and (i + 1) * 5 > start_minute}
    shares = opencost_step_shares(cost, [BASE + i * STEP for i in sorted(rows | running_steps)], STEP)
    assert sum(shares.values()) == pytest.approx(1.0)
    partial = opencost_step_shares(cost, [BASE + i * STEP for i in sorted(rows)], STEP)
    assert sum(partial.values()) <= 1.0 + 1e-9


class _WindowOpenCost:
    """OpenCost answering one accumulated allocation per queried window for a 7-minute job and a steady pod."""

    def __init__(self) -> None:
        self.last_status = "ok"

    async def collect_range(self, start, end):
        job_start, job_end = BASE + timedelta(minutes=2), BASE + timedelta(minutes=9)
        metrics = []
        overlap = (min(end, job_end) - max(start, job_start)).total_seconds()
        if overlap > 0:
            metrics.append(
                CostMetric(
                    pod_name="job-7min",
                    namespace="batch",
                    cpu_cost=0.1 * overlap / 60,
                    ram_cost=0.0,
                    total_cost=0.1 * overlap / 60,
                    window_start=start,
                    window_end=end,
                    running_start=max(start, job_start),
                    running_end=min(end, job_end),
                    minutes=overlap / 60,
                )
            )
        minutes = (end - start).total_seconds() / 60
        metrics.append(
            CostMetric(
                pod_name="steady",
                namespace="batch",
                cpu_cost=0.12 * minutes / 1,
                ram_cost=0.0,
                total_cost=0.12 * minutes,
                window_start=start,
                window_end=end,
                running_start=start,
                running_end=end,
                minutes=minutes,
            )
        )
        return metrics


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [True, False], ids=["one-10min-window", "two-5min-windows"])
async def test_stored_cost_does_not_depend_on_chunking(chunked):
    """S9: job-7min stores 0.70 in total (0.30 + 0.40), the steady pod 1.20, however the window is chunked."""
    cfg = Config(PROMETHEUS_QUERY_RANGE_STEP="5m", KUBERNETES_METRICS_API_ENABLED=False, CLOUD_PROVIDER="aws")
    prom = FakePrometheus()
    for pod in ("job-7min", "steady"):
        prom.add(
            "container_cpu_usage_seconds_total",
            {"namespace": "batch", "pod": pod, "container": "app", "node": "n1"},
            0.1,
        )
    stored = type("Stored", (), {"read_combined_metrics": staticmethod(lambda *a, **k: _empty())})()
    processor = build_range_processor(
        cfg, stored, prom, _WindowOpenCost(), pods=[("batch", "job-7min", "app"), ("batch", "steady", "app")]
    )
    windows = [(BASE, BASE + 2 * STEP)] if chunked else [(BASE, BASE + STEP), (BASE + STEP, BASE + 2 * STEP)]
    rows = []
    for start, end in windows:
        rows.extend(await processor.run_range(start, end))
    pods = [row for row in rows if row.container_name is None]
    job = {row.timestamp: row.total_cost for row in pods if row.pod_name == "job-7min"}
    steady = sum(row.total_cost or 0 for row in pods if row.pod_name == "steady")
    assert sum(job.values()) == pytest.approx(0.70, rel=0.01)
    assert job[BASE] == pytest.approx(0.30, rel=0.01)
    assert job[BASE + STEP] == pytest.approx(0.40, rel=0.01)
    assert steady == pytest.approx(1.20, rel=0.01)


async def _empty():
    return []
