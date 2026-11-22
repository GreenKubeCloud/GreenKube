"""WP-01.2 / COL-03: a pod without a CPU sample at a step still gets its row (cpu NULL, flagged)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.models.metrics import CostMetric, PodMetric
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
STEPS = 12
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
LATE_FROM = (BASE + timedelta(seconds=4 * STEP)).timestamp()  # first CPU sample describes step 3


class _OpenCost:
    """Each pod costs 1.20 for the hour and ran for all of it."""

    async def collect_range(self, start, end):
        return [
            CostMetric(pod_name=pod, namespace="ns", cpu_cost=0, ram_cost=0, total_cost=1.20, minutes=60)
            for pod in ("ok", "late", "memonly")
        ]


def _prometheus() -> FakePrometheus:
    prom = FakePrometheus()
    for pod in ("ok", "late", "memonly"):
        prom.add("kube_pod_info", {"namespace": "ns", "pod": pod, "node": "n1"}, 1.0)
        prom.add("kube_pod_status_phase", {"namespace": "ns", "pod": pod, "phase": "Running"}, 1.0)
    # A completed Job pod keeps kube_pod_info but is not Running: it gets no row.
    prom.add("kube_pod_info", {"namespace": "ns", "pod": "done", "node": "n1"}, 1.0)
    prom.add("kube_pod_status_phase", {"namespace": "ns", "pod": "done", "phase": "Succeeded"}, 1.0)
    prom.add("kube_pod_status_phase", {"namespace": "ns", "pod": "done", "phase": "Running"}, 0.0)
    ok = {"namespace": "ns", "pod": "ok", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", ok, 0.2)
    prom.add("container_memory_working_set_bytes", ok, 100 * 1024**2)
    late = {"namespace": "ns", "pod": "late", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", late, lambda ts: 0.2 if ts >= LATE_FROM else None)
    prom.add("container_memory_working_set_bytes", late, lambda ts: 50 * 1024**2 if ts >= LATE_FROM else None)
    memonly = {"namespace": "ns", "pod": "memonly", "node": "n1", "container": "app"}
    prom.add("container_memory_working_set_bytes", memonly, 20 * 1024**2)
    return prom


@pytest.mark.asyncio
async def test_pods_without_cpu_samples_keep_their_rows_and_cost(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    pods = [
        PodMetric(pod_name=pod, namespace="ns", container_name="app", cpu_request=100, memory_request=0)
        for pod in ("ok", "late", "memonly")
    ]
    processor = build_range_processor(cfg, MagicMock(), _prometheus(), _OpenCost(), pod_list=pods)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEPS * STEP))

    pod_rows = [row for row in rows if not row.container_name]
    by_pod = {pod: [row for row in pod_rows if row.pod_name == pod] for pod in ("ok", "late", "memonly")}
    assert len(by_pod["ok"]) == STEPS
    assert len(by_pod["late"]) == STEPS
    assert len(by_pod["memonly"]) == STEPS
    assert not [row for row in pod_rows if row.pod_name == "done"]
    without_cpu = [row for row in by_pod["late"] if row.cpu_usage_millicores is None]
    assert len(without_cpu) == 3
    assert all("no_cpu_sample" in row.estimation_reasons and row.is_estimated for row in without_cpu)
    assert all(row.cpu_usage_millicores is None for row in by_pod["memonly"])
    assert all(row.memory_usage_bytes == 20 * 1024**2 for row in by_pod["memonly"])
    assert sum(row.total_cost for row in pod_rows) == pytest.approx(3.60, rel=0.01)
    # Rows with a measured usage keep their usage provenance.
    assert all(row.cpu_usage_millicores == 200 for row in by_pod["ok"])
