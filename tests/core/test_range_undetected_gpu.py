"""GPU nodes without the DCGM exporter are detected and their GPU power estimated (NRG-13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.node_collector import NodeCollector
from greenkube.core import collection_telemetry
from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.impact.power import undetected_gpu_power
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
