"""USER BUG 2: Metrics API container usage never exceeds the pod cgroup that contains it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.metrics_server_collector import MetricsServerCollector
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.historical_range_processor import CONTAINER_USAGE_EXCEEDS_POD, HistoricalRangeProcessor
from greenkube.models.prometheus_metrics import ContainerResourceUsage
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

MIB = 1024 * 1024
STEP = timedelta(minutes=5)
POD = ("ns", "p")
POD_LABELS = {"namespace": "ns", "pod": "p", "node": "n1", "id": "/kubepods/burstable/podp"}

fit = HistoricalRangeProcessor._fit_to_pod_cgroup


class TestFitToPodCgroup:
    def test_containers_above_their_pod_are_scaled_to_it_keeping_their_split(self):
        usage = {("ns", "p", "app"): 750 * MIB, ("ns", "p", "sidecar"): 50 * MIB}
        refitted = fit(usage, set(usage), {POD: 600 * MIB}, set())
        assert refitted == set(usage)
        assert usage[("ns", "p", "app")] == pytest.approx(562.5 * MIB, abs=1)
        assert usage[("ns", "p", "sidecar")] == pytest.approx(37.5 * MIB, abs=1)
        assert sum(usage.values()) <= 600 * MIB

    def test_sampling_skew_within_tolerance_is_kept(self):
        usage = {("ns", "p", "app"): 620 * MIB}
        assert fit(usage, set(usage), {POD: 600 * MIB}, set()) == set()
        assert usage[("ns", "p", "app")] == 620 * MIB

    def test_pods_measured_per_container_by_prometheus_are_not_checked(self):
        # The pod value is then the sum of the Prometheus containers, not the cgroup: a
        # container filled from the Metrics API legitimately adds to it.
        usage = {("ns", "p", "app"): 500 * MIB, ("ns", "p", "late"): 300 * MIB}
        assert fit(usage, {("ns", "p", "late")}, {POD: 500 * MIB}, {POD}) == set()
        assert usage[("ns", "p", "late")] == 300 * MIB

    @pytest.mark.parametrize("pod_usage", [{}, {POD: 0}])
    def test_without_a_pod_value_nothing_can_be_checked(self, pod_usage):
        usage = {("ns", "p", "app"): 900 * MIB}
        assert fit(usage, set(usage), pod_usage, set()) == set()
        assert usage[("ns", "p", "app")] == 900 * MIB

    def test_only_filled_containers_are_refitted(self):
        usage = {("ns", "p", "app"): 900 * MIB}
        assert fit(usage, set(), {POD: 600 * MIB}, set()) == set()


class _MetricsApi(MetricsServerCollector):
    def __init__(self, cpu: int, memory: int) -> None:
        super().__init__()
        self.cpu = cpu
        self.memory = memory

    async def collect(self):
        return [
            ContainerResourceUsage(
                namespace="ns",
                pod_name="p",
                container_name="app",
                cpu_usage_millicores=self.cpu,
                memory_usage_bytes=self.memory,
                timestamp=datetime.now(timezone.utc),
                duration_seconds=60,
            )
        ]


@pytest.fixture
async def processor(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "refit.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=True,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    prom = FakePrometheus()
    # Pod-level cgroups only (no container label), as on Minikube with the Docker driver.
    prom.add("container_cpu_usage_seconds_total", POD_LABELS, 0.2)
    prom.add("container_memory_working_set_bytes", POD_LABELS, 560.0 * MIB)
    yield build_range_processor(cfg, SQLiteCombinedMetricsRepository(dbm), prom, FakeOpenCost())
    await dbm.close()


def _current_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, tz=timezone.utc)
    return end - 2 * STEP, end


@pytest.mark.asyncio
async def test_metrics_api_usage_above_the_pod_cgroup_is_refitted_and_flagged(processor):
    # The kubelet's container working set counts page cache: 756Mi in a 560Mi pod.
    processor._metrics_server_collector = _MetricsApi(cpu=350, memory=756 * MIB)

    rows = await processor.run_range(*_current_window())

    pod = max((row for row in rows if not row.container_name), key=lambda row: row.timestamp)
    [container] = [row for row in rows if row.container_name]
    assert pod.memory_usage_bytes == 560 * MIB
    assert container.memory_usage_bytes == 560 * MIB
    assert container.cpu_usage_millicores == 200
    assert container.is_estimated and CONTAINER_USAGE_EXCEEDS_POD in container.estimation_reasons


@pytest.mark.asyncio
async def test_metrics_api_usage_consistent_with_the_pod_is_kept_as_measured(processor):
    processor._metrics_server_collector = _MetricsApi(cpu=150, memory=500 * MIB)

    rows = await processor.run_range(*_current_window())

    [container] = [row for row in rows if row.container_name]
    assert container.memory_usage_bytes == 500 * MIB
    assert container.cpu_usage_millicores == 150
    assert CONTAINER_USAGE_EXCEEDS_POD not in container.estimation_reasons


@pytest.mark.asyncio
async def test_container_usage_from_the_metrics_api_says_so(processor):
    """COL-05 (S7): the container row is a Metrics API sample, the pod row a Prometheus measurement."""
    processor._metrics_server_collector = _MetricsApi(cpu=150, memory=500 * MIB)

    rows = await processor.run_range(*_current_window())

    [container] = [row for row in rows if row.container_name]
    pod = next(row for row in rows if not row.container_name and row.timestamp == container.timestamp)
    assert container.usage_source == "metrics_server"
    assert container.is_estimated and "metrics_api_point_sample" in container.estimation_reasons
    assert pod.usage_source == "prometheus"


@pytest.mark.asyncio
async def test_a_pod_cgroup_working_set_below_one_page_is_a_gap_not_a_measurement(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "gap.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    try:
        prom = FakePrometheus()
        prom.add("container_cpu_usage_seconds_total", POD_LABELS, 0.2)
        # Minikube's cAdvisor reported 8 bytes for hours (2026-11-08 14:00-16:00).
        prom.add("container_memory_working_set_bytes", POD_LABELS, 8.0)
        processor = build_range_processor(cfg, SQLiteCombinedMetricsRepository(dbm), prom, FakeOpenCost())

        rows = await processor.run_range(*_current_window())

        pods = [row for row in rows if not row.container_name]
        assert pods and all(row.memory_usage_bytes is None for row in pods)
    finally:
        await dbm.close()
