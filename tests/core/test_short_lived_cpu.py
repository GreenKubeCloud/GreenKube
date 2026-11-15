"""COL-03 step 3: CPU of a container too short-lived for rate() is recovered from its counter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.core.historical_range_processor import CPU_FROM_COUNTER_START
from greenkube.models.metrics import PodMetric
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


class _NoRate(FakePrometheus):
    """rate() needs two scrapes of one series: a 40 s container has none."""

    async def collect_range(self, start, end, step=None, query=None, **kwargs):
        if query and "rate(container_cpu_usage_seconds_total" in query:
            self.queries.append((query, 0.0, 0.0, step))
            return []
        return await super().collect_range(start, end, step=step, query=query, **kwargs)


def _processor(tmp_path, prom):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    pods = [PodMetric(pod_name="job", namespace="ns", container_name="run", cpu_request=100, memory_request=0)]
    return build_range_processor(cfg, MagicMock(), prom, FakeOpenCost(pods=[("ns", "job")]), pod_list=pods)


@pytest.mark.asyncio
async def test_container_living_40_seconds_gets_its_cpu_from_the_counter(tmp_path):
    prom = _NoRate()
    started = (BASE + timedelta(seconds=STEP)).timestamp()  # the container runs inside step 1 only
    labels = {"namespace": "ns", "pod": "job", "container": "run", "node": "n1"}
    # Evaluated at the end of step 1 the counter holds 20 CPU-seconds; the series is absent before.
    prom.add("container_cpu_usage_seconds_total", labels, lambda ts: 20.0 if started < ts <= started + STEP else None)
    # cAdvisor's start time proves the container started inside the step (40 s before its end).
    prom.add(
        "container_start_time_seconds", labels, lambda ts: started + 260 if started < ts <= started + STEP else None
    )
    processor = _processor(tmp_path, prom)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=3 * STEP))

    pod_rows = [row for row in rows if not row.container_name and row.pod_name == "job"]
    assert [row.timestamp for row in pod_rows] == [BASE + timedelta(seconds=STEP)]
    (row,) = pod_rows
    assert row.cpu_usage_millicores == 67
    assert CPU_FROM_COUNTER_START in row.estimation_reasons and row.is_estimated
    container_rows = [r for r in rows if r.container_name == "run"]
    assert [r.cpu_usage_millicores for r in container_rows] == [67]


@pytest.mark.asyncio
async def test_a_series_present_in_the_previous_step_is_not_read_as_a_start(tmp_path):
    prom = _NoRate()
    labels = {"namespace": "ns", "pod": "job", "container": "run", "node": "n1"}
    prom.add("container_cpu_usage_seconds_total", labels, 5000.0)  # long-running, rate missing (scrape gap)
    processor = _processor(tmp_path, prom)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=2 * STEP))

    assert not [row for row in rows if row.pod_name == "job" and row.cpu_usage_millicores is not None]


@pytest.mark.asyncio
async def test_a_scrape_gap_never_reads_a_long_running_counter_as_a_start(tmp_path):
    """Review 1: after a Prometheus restart the series reappears with its whole counter (50000 s)."""
    prom = _NoRate()
    labels = {"namespace": "ns", "pod": "job", "container": "run", "node": "n1"}
    gap_end = (BASE + timedelta(seconds=STEP)).timestamp()
    prom.add("container_cpu_usage_seconds_total", labels, lambda ts: 50000.0 if ts > gap_end else None)
    # The container started days ago.
    prom.add("container_start_time_seconds", labels, lambda ts: BASE.timestamp() - 86400 if ts > gap_end else None)
    processor = _processor(tmp_path, prom)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=3 * STEP))

    assert not [row for row in rows if row.pod_name == "job" and row.cpu_usage_millicores is not None]


@pytest.mark.asyncio
async def test_a_start_without_start_time_or_above_node_capacity_is_rejected(tmp_path):
    """Review 1: no cAdvisor start time, or more cores than the node has: no recovered CPU."""
    started = (BASE + timedelta(seconds=STEP)).timestamp()
    for counter, start_time in ((20.0, None), (5000.0, started + 10)):
        prom = _NoRate()
        labels = {"namespace": "ns", "pod": "job", "container": "run", "node": "n1"}
        prom.add(
            "container_cpu_usage_seconds_total",
            labels,
            lambda ts, value=counter: value if started < ts <= started + STEP else None,
        )
        if start_time is not None:
            prom.add(
                "container_start_time_seconds",
                labels,
                lambda ts, value=start_time: value if started < ts <= started + STEP else None,
            )
        processor = _processor(tmp_path, prom)

        rows = await processor.run_range(BASE, BASE + timedelta(seconds=3 * STEP))

        # 5000 CPU-seconds in 300 s is 16.7 cores on a 2-core node.
        assert not [row for row in rows if row.pod_name == "job" and row.cpu_usage_millicores is not None]
