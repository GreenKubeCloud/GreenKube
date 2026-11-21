"""COL-12: memory is the step average for allocation and the step peak for rightsizing.

A single instant value per step (the working set at the end of the step)
hides the peaks between two evaluation points: with a 5-minute step one
scrape out of ten was kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.models.metrics import PodMetric
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
MI = 1024 * 1024
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_spike_between_evaluations_is_the_peak_and_counts_in_the_average(tmp_path):
    prom = FakePrometheus(window_scrape_seconds=30)
    labels = {"namespace": "ns", "pod": "web", "container": "app", "node": "n1", "name": "c1"}
    spike = (BASE + timedelta(seconds=STEP + 120)).timestamp()  # inside the step of row BASE + STEP
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, lambda ts: 400 * MI if abs(ts - spike) < 1 else 100 * MI)
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    pods = [PodMetric(pod_name="web", namespace="ns", container_name="app", cpu_request=100, memory_request=0)]
    processor = build_range_processor(cfg, MagicMock(), prom, FakeOpenCost(pods=[("ns", "web")]), pod_list=pods)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=3 * STEP))

    containers = {r.timestamp: r for r in rows if r.container_name == "app"}
    spiked = containers[BASE + timedelta(seconds=STEP)]
    assert spiked.memory_usage_max_bytes == 400 * MI
    assert spiked.memory_usage_bytes == 130 * MI
    calm = containers[BASE]
    assert (calm.memory_usage_bytes, calm.memory_usage_max_bytes) == (100 * MI, 100 * MI)
    pod = next(r for r in rows if not r.container_name and r.timestamp == BASE + timedelta(seconds=STEP))
    assert pod.memory_usage_bytes == 130 * MI
