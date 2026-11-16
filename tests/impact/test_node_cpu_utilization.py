"""Node utilisation includes CPU spent outside pods (kubelet, runtime, kernel) (NRG-12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.impact.rows import MetricRowAllocator, NodeSpec
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
STEP = 300
BASE = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)


def _pod(name: str, usage_m: int) -> CombinedMetric:
    return CombinedMetric(
        pod_name=name,
        namespace="ns",
        node="n1",
        timestamp=NOW,
        duration_seconds=3600,
        cpu_request=usage_m,
        memory_request=0,
        cpu_usage_millicores=usage_m,
    )


def _allocate(node_cpu_used_cores):
    allocator = MetricRowAllocator(Config(POWER_MEMORY_W_PER_GIB=0.0))
    spec = NodeSpec(
        name="n1",
        cpu_capacity_cores=4,
        idle_watts=4.0,
        max_watts=14.0,
        vcores=4,
        power_source="node_capacity_derived",
        node_cpu_used_cores=node_cpu_used_cores,
    )
    rows = [_pod("a", 600), _pod("b", 400)]
    profile = allocator.node_profile(spec, rows, pue=1.0, intensity=100.0, hours=1.0)
    return allocator.allocate(rows, profile, 1.0, emit_idle=True, idle_fields={"timestamp": NOW})


def test_root_cgroup_usage_sets_utilisation_and_the_system_share_goes_to_idle():
    """Acceptance a: 4 cores, pods use 1.0, root cgroup 1.4 -> utilisation 0.35."""
    window = _allocate(1.4)
    dynamic_j = 0.35 * (14.0 - 4.0) * 3600
    pods = {row.pod_name: row for row in window.rows}
    idle_j = 4.0 * 3600
    # Pods: their reserved idle share (1 of 4 cores) and pod_i / 1.4 of the dynamic energy.
    assert pods["a"].joules == pytest.approx(0.6 / 4 * idle_j + 0.6 / 1.4 * dynamic_j)
    assert pods["b"].joules == pytest.approx(0.4 / 4 * idle_j + 0.4 / 1.4 * dynamic_j)
    assert window.idle is not None
    assert window.idle.joules == pytest.approx(3.0 / 4 * idle_j + 0.4 / 1.4 * dynamic_j)
    total = sum(row.joules for row in window.rows) + window.idle.joules
    assert total == pytest.approx(idle_j + dynamic_j)


def test_without_node_series_the_result_is_the_pod_sum():
    """Acceptance b."""
    window = _allocate(None)
    assert window.idle is not None
    total = sum(row.joules for row in window.rows) + window.idle.joules
    assert total == pytest.approx((4.0 + 0.25 * 10.0) * 3600)


def test_node_usage_below_the_pod_sum_never_lowers_utilisation():
    window = _allocate(0.5)
    assert window.idle is not None
    total = sum(row.joules for row in window.rows) + window.idle.joules
    assert total == pytest.approx((4.0 + 0.25 * 10.0) * 3600)


async def test_run_range_reads_the_root_cgroup_usage_of_each_node(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )

    async def run(root_cores):
        prom = FakePrometheus()
        labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": "app"}
        prom.add("container_cpu_usage_seconds_total", labels, 1.0)
        prom.add("container_memory_working_set_bytes", labels, 1024**2)
        if root_cores is not None:
            prom.add("container_cpu_usage_seconds_total", {"id": "/", "node": "n1"}, root_cores)
        node = NodeInfo(name="n1", cloud_provider="unknown", instance_type="cpu-4", cpu_capacity_cores=4)
        processor = build_range_processor(
            cfg, MagicMock(), prom, FakeOpenCost(), nodes={"n1": node}, zone_mapper=NodeZoneMapper(MagicMock(), cfg)
        )
        rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
        pods = [row for row in rows if not row.container_name]
        return pods, processor.last_idle_metrics

    pods_only, idle_only = await run(None)
    pods_root, idle_root = await run(2.0)
    total_only = sum(r.joules for r in pods_only) + sum(r.joules for r in idle_only)
    total_root = sum(r.joules for r in pods_root) + sum(r.joules for r in idle_root)
    # The pod keeps pod / capacity of the dynamic range (1.0 / 2.0 of a doubled utilisation);
    # the 1.0 core used outside pods goes to the idle row as the system share.
    assert pods_root[0].joules == pytest.approx(pods_only[0].joules)
    assert total_root > total_only
    assert idle_root[0].joules > idle_only[0].joules
