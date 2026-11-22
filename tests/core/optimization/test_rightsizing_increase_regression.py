"""USER BUG 2, Prometheus-only shapes: absurd increases from summed or pod-level series.

The reported case itself (Metrics API container working sets that count page
cache) is replayed in ``test_rightsizing_metrics_api_regression.py``. This
module covers the Prometheus-only paths with the same workload.

The fixture holds real ``query_range`` responses recorded on the Minikube
cluster: ``monitoring/prometheus-k8s`` (StatefulSet, 2 replicas), container
``prometheus`` requesting 400Mi and ``config-reloader`` 50Mi. There, cAdvisor
only exports pod-level cgroups (no ``container`` label).

Two cluster shapes are replayed through the real collection pipeline
(:class:`HistoricalRangeProcessor`), the usage rollup (Python reference and
SQLite SQL) and the memory analyzer, with the Metrics API disabled:

* **duplicated container series** (defensive, not the reported cluster): each
  container's working set is exported twice (kubelet ``/metrics/cadvisor`` and
  ``/metrics/resource``), as on clusters scraping both endpoints. The collector
  summed the duplicates: ~1.2Gi per replica, sized to ~1.5Gi. Fixed:
  duplicates are merged with max.
* **pod-level cgroups only** (the recorded shape, without Metrics API
  samples): usage exists for the pod, never for a container; no
  container-level increase may be invented from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.workload_usage import WorkloadUsageRollup, summarize_rows
from greenkube.models.metrics import PodMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

MIB = 1024 * 1024
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "prometheus_k8s_minikube.json"
NAMESPACE = "monitoring"
PODS = ("prometheus-k8s-0", "prometheus-k8s-1")
TARGET = (NAMESPACE, "StatefulSet", "prometheus-k8s")
PROMETHEUS_REQUEST = 400 * MIB
RELOADER_REQUEST = 50 * MIB
RELOADER_WORKING_SET = 14 * MIB
STEP = 300
#: Skip reason of a pod-level-only target (``rightsizing.POD_LEVEL_USAGE_ONLY``).
POD_LEVEL_USAGE_ONLY = "pod_level_usage_only"


def _fixture() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _working_set(fixture: Dict[str, Any]) -> Dict[str, Dict[float, float]]:
    """Recorded pod working set per pod and timestamp, while the pods were running."""
    series: Dict[str, Dict[float, float]] = {}
    for item in fixture["working_set"]:
        values = {float(ts): float(value) for ts, value in item["values"] if float(value) > MIB}
        series[item["metric"]["pod"]] = values
    return series


def _recorded_requests(fixture: Dict[str, Any]) -> Dict[tuple, int]:
    return {
        (item["metric"]["pod"], item["metric"]["container"]): int(float(item["values"][-1][1]))
        for item in fixture["requests"]
    }


def _lookup(values: Dict[float, float], transform: Callable[[float], float]) -> Any:
    """Recorded value at a range timestamp; None (no sample) when Prometheus had none."""

    def at(ts: float) -> Optional[float]:
        value = values.get(float(ts))
        return None if value is None else transform(value)

    return at


def _running_window(series: Dict[str, Dict[float, float]]) -> tuple[datetime, datetime]:
    """The longest run of consecutive 5-minute samples present for every pod."""
    common = sorted(set.intersection(*(set(values) for values in series.values())))
    best: List[float] = []
    run: List[float] = []
    for ts in common:
        if run and ts - run[-1] != STEP:
            run = []
        run.append(ts)
        if len(run) > len(best):
            best = list(run)
    assert len(best) >= 24, "the fixture must hold at least two hours of consecutive samples"
    # A sample evaluated at t is stored at t - step (A2): the window covers [first - step, last).
    start = datetime.fromtimestamp(best[0] - STEP, tz=timezone.utc)
    end = datetime.fromtimestamp(best[-1], tz=timezone.utc)
    return start, end


def _prometheus(fixture: Dict[str, Any], *, shape: str) -> FakePrometheus:
    prom = FakePrometheus()
    series = _working_set(fixture)
    for pod in PODS:
        values = series[pod]
        base = {"namespace": NAMESPACE, "pod": pod, "node": "minikube"}
        prom.add("kube_pod_container_status_restarts_total", {**base, "container": "prometheus"}, 72.0)
        if shape == "pod_level":
            # Recorded shape: one pod cgroup series per pod, no container label.
            prom.add(
                "container_memory_working_set_bytes",
                {**base, "id": f"/kubepods/burstable/{pod}"},
                _lookup(values, float),
            )
            prom.add("container_cpu_usage_seconds_total", {**base, "id": f"/kubepods/burstable/{pod}"}, 0.02)
            continue
        # kubeadm shape: per-container series, each exported by two kubelet endpoints.
        for path in ("/metrics/cadvisor", "/metrics/resource"):
            labels = {**base, "metrics_path": path}
            prometheus = _lookup(values, lambda value: value - RELOADER_WORKING_SET)
            prom.add("container_memory_working_set_bytes", {**labels, "container": "prometheus"}, prometheus)
            prom.add(
                "container_memory_working_set_bytes", {**labels, "container": "config-reloader"}, RELOADER_WORKING_SET
            )
            prom.add("container_cpu_usage_seconds_total", {**labels, "container": "prometheus"}, 0.02)
            prom.add("container_cpu_usage_seconds_total", {**labels, "container": "config-reloader"}, 0.001)
    return prom


def _pod_metrics(fixture: Dict[str, Any]) -> List[PodMetric]:
    requests = _recorded_requests(fixture)
    assert requests[("prometheus-k8s-0", "prometheus")] == PROMETHEUS_REQUEST
    assert requests[("prometheus-k8s-0", "config-reloader")] == RELOADER_REQUEST
    return [
        PodMetric(
            pod_name=pod,
            namespace=NAMESPACE,
            container_name=container,
            cpu_request=100 if container == "prometheus" else 10,
            memory_request=requests[(pod, container)],
            owner_kind="StatefulSet",
            owner_name="prometheus-k8s",
        )
        for pod in PODS
        for container in ("prometheus", "config-reloader")
    ]


@pytest.fixture
async def pipeline(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "bug2.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    repo = SQLiteCombinedMetricsRepository(dbm)
    fixture = _fixture()

    async def run(shape: str):
        processor = build_range_processor(
            cfg,
            repo,
            _prometheus(fixture, shape=shape),
            FakeOpenCost(pods=[(NAMESPACE, pod) for pod in PODS]),
            nodes={
                "minikube": NodeInfo(
                    name="minikube", instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=4
                )
            },
        )
        processor.pod_collector.collect.return_value = _pod_metrics(fixture)
        start, end = _running_window(_working_set(fixture))
        rows = await processor.run_range(start, end)
        await repo.write_combined_metrics(rows)
        return rows, end

    try:
        yield cfg, dbm, run
    finally:
        await dbm.close()


def _memory_recs(cfg: Config, rows, usage=None):
    pods = [row for row in rows if not row.container_name]
    containers = [row for row in rows if row.container_name]
    context = OptimizationContext(config=cfg, metrics=pods, container_metrics=containers, usage=usage)
    analyzer = MemoryRightsizingAnalyzer(cfg)
    return analyzer.analyze(context), analyzer, context


@pytest.mark.asyncio
async def test_duplicated_container_series_are_not_summed_into_absurd_increases(pipeline):
    cfg, dbm, run = pipeline
    rows, end = await run("kubeadm_duplicates")

    containers = [row for row in rows if row.container_name == "prometheus"]
    assert containers
    peak = max(row.memory_usage_bytes or 0 for row in containers)
    # One container's working set, never the sum of its duplicate series (~1.25Gi on dev).
    assert 600 * MIB < peak < 650 * MIB

    reference = summarize_rows(
        [row for row in rows if not row.container_name], [row for row in rows if row.container_name], step_seconds=STEP
    )
    usage_repo = create_workload_usage_repository(dbm, "sqlite")
    await WorkloadUsageRollup(usage_repo, step_seconds=STEP, backfill_hours=48).refresh(end)
    sql = await usage_repo.read_usage_snapshot(end - timedelta(days=2), end + timedelta(hours=1))

    for snapshot in (reference, sql):
        summary = snapshot.summaries[TARGET]["prometheus"]
        assert summary.memory is not None
        # Per container and per replica: the hottest replica's p99, not 2 replicas or 2 series summed.
        assert 590 * MIB < summary.memory.p99 < 650 * MIB
        assert summary.memory_request == PROMETHEUS_REQUEST

        recs, _analyzer, _context = _memory_recs(cfg, rows, usage=snapshot)
        by_container = {rec.container_name: rec for rec in recs}
        rec = by_container["prometheus"]
        assert rec.current_memory_request_bytes == PROMETHEUS_REQUEST
        recommended = rec.recommended_memory_request_bytes
        assert recommended is not None
        # p99 (~610-640Mi) x 1.2 headroom: a defensible step, well below 1.2Gi and within 2x the request.
        assert 700 * MIB <= recommended <= 2 * PROMETHEUS_REQUEST
        assert recommended < 1024 * MIB
        assert "per-replica p99" in rec.reason
        assert rec.potential_savings_cost is not None and rec.potential_savings_cost <= 0
        # The reloader is far below its request: no increase for it.
        assert "config-reloader" not in by_container or (
            (by_container["config-reloader"].recommended_memory_request_bytes or 0) < RELOADER_REQUEST
        )


@pytest.mark.asyncio
async def test_pod_level_cgroups_only_never_invent_container_increases(pipeline):
    cfg, _dbm, run = pipeline
    rows, _end = await run("pod_level")

    pods = [row for row in rows if not row.container_name]
    assert pods and not [row for row in rows if row.container_name]
    # The pod cgroup working set is collected (it was dropped before), at pod level only.
    assert all(row.memory_usage_bytes and row.memory_usage_bytes > 300 * MIB for row in pods)

    recs, analyzer, _context = _memory_recs(cfg, rows)
    assert [
        rec for rec in recs if (rec.recommended_memory_request_bytes or 0) > (rec.current_memory_request_bytes or 0)
    ] == []
    assert (TARGET, None, "memory", POD_LEVEL_USAGE_ONLY) in analyzer.skipped

    context = OptimizationContext(config=cfg, metrics=pods)
    assert CpuRightsizingAnalyzer(cfg).analyze(context) == [] or all(
        (rec.recommended_cpu_request_millicores or 0) < (rec.current_cpu_request_millicores or 0)
        for rec in CpuRightsizingAnalyzer(cfg).analyze(context)
    )
