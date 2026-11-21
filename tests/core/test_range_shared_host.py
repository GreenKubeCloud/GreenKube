"""run_range charges nodes of one physical host the host once (NRG-15)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)
GIB = 1024**3


def _node(name: str, boot_id: str) -> NodeInfo:
    return NodeInfo(
        name=name,
        instance_type="cpu-14",
        cloud_provider="unknown",
        cpu_capacity_cores=14,
        memory_capacity_bytes=39 * GIB,
        host_id=boot_id,
    )


async def _idle_joules(tmp_path, nodes: dict[str, NodeInfo], *, detection: bool = True) -> tuple[float, list]:
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
        SHARED_HOST_DETECTION=detection,
    )
    prom = FakePrometheus()
    pods = []
    for name in nodes:
        labels = {"namespace": "ns", "pod": f"p-{name}", "node": name, "container": "app"}
        prom.add("container_cpu_usage_seconds_total", labels, 0.0)
        prom.add("container_memory_working_set_bytes", labels, 0)
        pods.append(("ns", f"p-{name}", "app"))
    processor = build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost([(ns, pod) for ns, pod, _ in pods]),
        pods=pods,
        nodes=nodes,
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    pod_rows = [row for row in rows if not row.container_name]
    idle = processor.last_idle_metrics
    return sum(row.joules for row in pod_rows) + sum(row.joules for row in idle), pod_rows


@pytest.mark.asyncio
async def test_two_nodes_with_one_boot_id_cost_one_host(tmp_path):
    nodes = {"cp": _node("cp", "boot-1"), "w1": _node("w1", "boot-1")}
    shared, pod_rows = await _idle_joules(tmp_path, nodes)
    alone, _ = await _idle_joules(tmp_path, nodes, detection=False)
    assert shared == pytest.approx(alone / 2, rel=1e-6)
    assert all(any(r.startswith("Shared host") for r in row.estimation_reasons) for row in pod_rows)


@pytest.mark.asyncio
async def test_distinct_boot_ids_are_not_scaled(tmp_path):
    nodes = {"a": _node("a", "boot-a"), "b": _node("b", "boot-b")}
    on, pod_rows = await _idle_joules(tmp_path, nodes)
    off, _ = await _idle_joules(tmp_path, nodes, detection=False)
    assert on == pytest.approx(off, rel=1e-9)
    assert not any(any(r.startswith("Shared host") for r in row.estimation_reasons) for row in pod_rows)
