"""COL-09: pods with pod-level cAdvisor series only are kept when other pods have container series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.models.metrics import PodMetric
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
STEPS = 12
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_mixed_runtimes_keep_pod_level_pods_and_their_node(tmp_path):
    """Scenario S2: n1 runs containerd (container series), n2 runs cri-dockerd (pod cgroup series only)."""
    prom = FakePrometheus()
    containerd = {"namespace": "ns", "pod": "on-containerd", "node": "n1"}
    prom.add("container_cpu_usage_seconds_total", {**containerd, "container": "app"}, 0.5)
    prom.add("container_cpu_usage_seconds_total", containerd, 0.5)  # its pod cgroup: never added on top
    prom.add("container_memory_working_set_bytes", {**containerd, "container": "app"}, 100 * 1024**2)
    dockerd = {"namespace": "ns", "pod": "on-cri-dockerd", "node": "n2"}
    prom.add("container_cpu_usage_seconds_total", dockerd, 0.7)
    prom.add("container_memory_working_set_bytes", dockerd, 200 * 1024**2)
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    nodes = {
        name: NodeInfo(name=name, instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=2)
        for name in ("n1", "n2")
    }
    pods = [
        PodMetric(pod_name=pod, namespace="ns", container_name="app", cpu_request=100, memory_request=0)
        for pod in ("on-containerd", "on-cri-dockerd")
    ]
    processor = build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost(pods=[("ns", "on-containerd"), ("ns", "on-cri-dockerd")]),
        pod_list=pods,
        nodes=nodes,
    )

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEPS * STEP))

    pod_rows = [row for row in rows if not row.container_name]
    dockerd_rows = [row for row in pod_rows if row.pod_name == "on-cri-dockerd"]
    assert len(dockerd_rows) == STEPS
    assert {row.cpu_usage_millicores for row in dockerd_rows} == {700}
    assert {row.memory_usage_bytes for row in dockerd_rows} == {200 * 1024**2}
    containerd_rows = [row for row in pod_rows if row.pod_name == "on-containerd"]
    assert {row.cpu_usage_millicores for row in containerd_rows} == {500}
    idle_nodes = sorted({row.node for row in processor.last_idle_metrics})
    assert idle_nodes == ["n1", "n2"]
    assert len([row for row in processor.last_idle_metrics if row.node == "n2"]) == STEPS
