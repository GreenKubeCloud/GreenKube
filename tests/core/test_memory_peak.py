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


def _spiky_prometheus(container: bool = True) -> FakePrometheus:
    """Working set at 240Mi with one 400Mi scrape in every step."""
    prom = FakePrometheus(window_scrape_seconds=30)
    labels = {"namespace": "ns", "pod": "web", "node": "n1", "container": "app" if container else ""}
    if container:
        labels["name"] = "c1"
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, lambda ts: 400 * MI if int(ts) % STEP == 120 else 240 * MI)
    return prom


def _range_processor(tmp_path, prom, memory_request=0):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    pods = [
        PodMetric(pod_name="web", namespace="ns", container_name="app", cpu_request=100, memory_request=memory_request)
    ]
    return build_range_processor(cfg, MagicMock(), prom, FakeOpenCost(pods=[("ns", "web")]), pod_list=pods)


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [True, False])
async def test_pod_rows_carry_the_step_peak(tmp_path, container):
    """Review 1: the pod row holds the sum of its container peaks, or its pod cgroup peak."""
    rows = await _range_processor(tmp_path, _spiky_prometheus(container)).run_range(
        BASE, BASE + timedelta(seconds=2 * STEP)
    )

    pods = [r for r in rows if not r.container_name]
    assert pods and all(r.memory_usage_max_bytes == 400 * MI for r in pods)


@pytest.mark.asyncio
async def test_a_spiky_container_is_never_sized_below_its_peak(tmp_path):
    """Review 1 reproduction: avg 240Mi, peak 400Mi, request 256Mi was raised to 288Mi only."""
    from greenkube.core.optimization.analyzers.rightsizing import memory_proposal
    from greenkube.core.workload_usage import rollup_rows, summarize

    rows = await _range_processor(tmp_path, _spiky_prometheus(), memory_request=256 * MI).run_range(
        BASE, BASE + timedelta(seconds=12 * STEP)
    )
    pod_rows = [r for r in rows if not r.container_name]
    container_rows = [r for r in rows if r.container_name]
    snapshot = summarize(rollup_rows(pod_rows, container_rows))
    (by_container,) = snapshot.summaries.values()

    proposal = memory_proposal(Config(), by_container["app"], by_container[""])

    assert proposal is not None and proposal.direction == "increase"
    assert proposal.proposed >= 400 * MI


def test_replica_p95_reads_the_step_peak_on_the_rows_path():
    """Review 1: the per-replica headroom guard takes max(average, peak) per row."""
    from greenkube.core.optimization.enrich import _replica_p95_max
    from greenkube.models.metrics import CombinedMetric

    rows = [
        CombinedMetric(
            pod_name="web",
            namespace="ns",
            timestamp=BASE + timedelta(seconds=k * STEP),
            memory_usage_bytes=240 * MI,
            memory_usage_max_bytes=400 * MI,
        )
        for k in range(20)
    ]
    assert _replica_p95_max(rows, "memory_usage_bytes") == 400 * MI


def test_bounding_by_the_pod_compares_peak_with_peak():
    """Review 1: a container peak within its pod peak is not cut to the pod averages."""
    from greenkube.core.optimization.analyzers.rightsizing import bounded_by_pod
    from greenkube.models.workload_usage import UsageStats

    container = UsageStats(
        sample_count=12, avg=240 * MI, p50=240 * MI, p90=240 * MI, p95=240 * MI, p99=400 * MI, max=400 * MI
    )
    pod = UsageStats(
        sample_count=12, avg=240 * MI, p50=240 * MI, p90=240 * MI, p95=240 * MI, p99=400 * MI, max=400 * MI
    )
    assert bounded_by_pod(container, pod) == (container, False)
    # A container above its pod's peak (Metrics API page cache) is still bounded.
    inflated = UsageStats(
        sample_count=12, avg=900 * MI, p50=900 * MI, p90=900 * MI, p95=900 * MI, p99=900 * MI, max=900 * MI
    )
    bounded, was_bounded = bounded_by_pod(inflated, pod)
    assert was_bounded and bounded is not None and bounded.max == 400 * MI


def test_evidence_percentiles_use_the_step_peak():
    """Re-review 4: evidence p95/p99 follow the sizing basis, max(average, peak) per raw step."""
    from greenkube.core.optimization.statistics import usage_stats
    from greenkube.models.metrics import CombinedMetric

    rows = [
        CombinedMetric(
            pod_name="web",
            namespace="ns",
            container_name="app",
            timestamp=BASE + timedelta(seconds=k * STEP),
            memory_usage_bytes=240 * MI,
            memory_usage_max_bytes=400 * MI,
        )
        for k in range(20)
    ]
    avg, peak, points = usage_stats(rows, "memory_usage_bytes", "memory_usage_max_bytes")
    assert avg == 240 * MI and peak == 400 * MI
    assert min(points) == 400 * MI
