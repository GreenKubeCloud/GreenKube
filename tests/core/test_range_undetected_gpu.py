"""GPU nodes without the DCGM exporter are detected and their GPU power estimated (NRG-13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.node_collector import NodeCollector
from greenkube.collectors.pod_collector import _container_metric
from greenkube.core import collection_telemetry
from greenkube.core.config import Config
from greenkube.core.historical_range_processor import RangeCollectionError
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.impact.power import undetected_gpu_power
from greenkube.models.metrics import PodMetric
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)


def test_known_model_without_dcgm_gets_the_idle_floor_of_each_gpu():
    estimate = undetected_gpu_power(4, "Tesla-T4")
    assert estimate.watts == pytest.approx(4 * 10.5)
    assert estimate.reason.startswith("GPU power estimated from TDP")


def test_unknown_model_is_flagged_unmeasured():
    estimate = undetected_gpu_power(2, "Mystery-X")
    assert estimate.watts is None
    assert "not measured" in estimate.reason


async def test_node_collector_reads_gpu_capacity_and_product_label():
    node = SimpleNamespace(
        metadata=SimpleNamespace(name="g1", labels={"nvidia.com/gpu.product": "Tesla-T4"}, annotations={}),
        spec=SimpleNamespace(provider_id=None),
        status=SimpleNamespace(capacity={"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "4"}, node_info=None),
    )
    api = MagicMock()
    api.list_node = AsyncMock(return_value=SimpleNamespace(items=[node], metadata=SimpleNamespace(_continue=None)))
    collector = NodeCollector()
    collector._api = api
    info = (await collector.collect())["g1"]
    assert (info.gpu_count, info.gpu_model) == (4, "Tesla-T4")


async def _run(tmp_path, node: NodeInfo):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": node.name, "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, 1024**2)
    processor = build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost(),
        nodes={node.name: node},
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    return [row for row in rows if not row.container_name], processor.last_idle_metrics


async def test_gpu_node_without_dcgm_carries_the_idle_gpu_power_and_a_reason(tmp_path):
    node = NodeInfo(name="g1", cloud_provider="aws", instance_type="g4dn.12xlarge", cpu_capacity_cores=48)
    node.gpu_count, node.gpu_model = 4, "Tesla-T4"
    pods, idle = await _run(tmp_path, node)
    (idle_row,) = idle
    assert idle_row.gpu_power_watts == pytest.approx(4 * 10.5)
    assert all(any(r.startswith("GPU power estimated from TDP") for r in row.estimation_reasons) for row in pods)


async def test_gpu_node_of_unknown_model_is_counted_unmeasured(tmp_path):
    node = NodeInfo(name="g1", cloud_provider="aws", instance_type="g9.mystery", cpu_capacity_cores=48)
    node.gpu_count, node.gpu_model = 2, "Mystery-X"
    pods, _ = await _run(tmp_path, node)
    assert collection_telemetry.GPU_NODES_UNMEASURED._value.get() == 1
    assert all(any("GPU power not measured" in r for r in row.estimation_reasons) for row in pods)


def _dcgm_processor(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )
    node = NodeInfo(name="g1", cloud_provider="aws", instance_type="g4dn.12xlarge", cpu_capacity_cores=48)
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": node.name, "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, 1024**2)
    gpu = {"UUID": "GPU-0", "modelName": "Tesla T4", "Hostname": node.name, "namespace": "ns", "pod": "p"}
    prom.add("DCGM_FI_DEV_POWER_USAGE", gpu, 40.0)
    prom.add("DCGM_FI_DEV_GPU_UTIL", gpu, 50.0)
    processor = build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost(),
        nodes={node.name: node},
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )
    return processor, prom


async def test_dcgm_outage_after_gpu_series_were_seen_fails_the_window(tmp_path):
    processor, prom = _dcgm_processor(tmp_path)
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    assert any(row.gpu_power_watts for row in rows)
    prom.fail_queries_containing = ("DCGM_FI_DEV",)
    later = BASE + timedelta(seconds=STEP)
    with pytest.raises(RangeCollectionError, match="DCGM"):
        await processor.run_range(later, later + timedelta(seconds=STEP))


async def test_dcgm_error_on_a_cluster_without_gpu_series_does_not_fail_the_window(tmp_path):
    processor, prom = _dcgm_processor(tmp_path)
    prom.fail_queries_containing = ("DCGM_FI_DEV",)
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    assert rows


def test_requested_gpus_are_charged_to_the_requesting_pod_at_the_assumed_utilisation():
    estimate = undetected_gpu_power(4, "Tesla-T4")
    by_pod, unattributed = estimate.split({("ns", "p"): 1}, assumed_utilization_pct=50.0)
    assert by_pod == {("ns", "p"): pytest.approx(10.5 + 0.5 * (70 - 10.5))}
    assert unattributed == pytest.approx(3 * 10.5)


def test_requests_beyond_the_node_capacity_are_capped():
    estimate = undetected_gpu_power(1, "Tesla-T4")
    by_pod, unattributed = estimate.split({("ns", "a"): 1, ("ns", "b"): 1}, assumed_utilization_pct=50.0)
    assert sum(by_pod.values()) == pytest.approx(10.5 + 0.5 * (70 - 10.5))
    assert unattributed == 0.0


def test_unknown_model_splits_nothing():
    by_pod, unattributed = undetected_gpu_power(2, "Mystery-X").split({("ns", "p"): 1}, assumed_utilization_pct=50.0)
    assert (by_pod, unattributed) == ({}, 0.0)


def test_pod_collector_reads_gpu_requests_and_limits():
    container = SimpleNamespace(
        name="app",
        resources=SimpleNamespace(requests={"cpu": "1"}, limits={"nvidia.com/gpu": "2"}),
    )
    metric = _container_metric(
        container, kind="container", pod_name="p", namespace="ns", owner_kind=None, owner_name=None
    )
    assert metric.gpu_request == 2


async def test_pod_requesting_a_gpu_on_a_node_without_dcgm_gets_its_gpu_power(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )
    node = NodeInfo(name="g1", cloud_provider="aws", instance_type="g4dn.12xlarge", cpu_capacity_cores=48)
    node.gpu_count, node.gpu_model = 4, "Tesla-T4"
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": node.name, "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, 1024**2)
    pod = PodMetric(
        pod_name="p", namespace="ns", container_name="app", cpu_request=1000, memory_request=0, gpu_request=1
    )
    processor = build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost(),
        nodes={node.name: node},
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
        pod_list=[pod],
    )
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    (pod_row,) = [row for row in rows if not row.container_name]
    assert pod_row.gpu_power_watts == pytest.approx(10.5 + 0.5 * (70 - 10.5))
    (idle_row,) = processor.last_idle_metrics
    assert idle_row.gpu_power_watts == pytest.approx(3 * 10.5)


def test_pod_snapshots_keep_gpu_requests_without_changing_other_pods():
    from greenkube.storage.base_pod_snapshot_repository import snapshots_from_pod_metrics

    seen = datetime(2026, 11, 7, tzinfo=timezone.utc)
    gpu_pod = PodMetric(
        pod_name="g", namespace="ns", container_name="app", cpu_request=1, memory_request=1, gpu_request=2
    )
    cpu_pod = PodMetric(pod_name="c", namespace="ns", container_name="app", cpu_request=1, memory_request=1)
    snapshots = {s.pod_name: s for s in snapshots_from_pod_metrics([gpu_pod, cpu_pod], seen)}
    assert [m.gpu_request for m in snapshots["g"].pod_metrics()] == [2]
    assert "gpu_request" not in snapshots["c"].containers[0]
