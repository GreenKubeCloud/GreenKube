"""USER BUG 2, real data path: ``prometheus`` proposed to go from 400Mi to ~1.2Gi.

The reported recommendation (Minikube, 2026-11-08 16:50: ``prometheus-k8s-0``
and ``-1`` container ``prometheus`` 400Mi -> 1287Mi and 1214Mi) was sized from
Metrics API samples. On that cluster (Docker driver, cri-dockerd) cAdvisor only
exports pod cgroups, so a container's usage only comes from the Metrics API
gap-fill of each scan, and that container working set includes page cache the
pod cgroup's working set excludes: the recorded kubelet snapshot reports
~756Mi for the ``prometheus`` container of a pod whose own working set is
~566Mi. A container can never use more than its pod.

The fixture holds real Prometheus ``query_range`` responses for the morning of
2026-11-08 (pod cgroup working set and usage, 30s step) and the kubelet
snapshot showing the contradiction. The replay runs one collection scan per
minute through the real :class:`HistoricalRangeProcessor` with Metrics API
gap-filling enabled: each scan's Metrics API sample for ``prometheus`` is the
recorded pod usage minus the reloader, the relation the snapshot shows
(726Mi + 15Mi against a 791Mi pod usage). Rows then go through the usage
rollup (Python reference and SQLite SQL) and the memory analyzer.

Before the fix the replay recommends ~1.1Gi per replica (p99 ~940Mi x 1.2),
the reported magnitude; the reported 1.2-1.3Gi came two hours later, once the
page cache had grown further. After the fix the container usage is refitted to
its pod cgroup and the increase is sized from the real working set (~750Mi),
without the step cap having to step in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import greenkube.core.historical_range_processor as range_module
from greenkube.collectors.metrics_server_collector import MetricsServerCollector
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers import MemoryRightsizingAnalyzer
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.workload_usage import WorkloadUsageRollup, summarize_rows
from greenkube.models.metrics import PodMetric, Recommendation
from greenkube.models.node import NodeInfo
from greenkube.models.prometheus_metrics import ContainerResourceUsage
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

MIB = 1024 * 1024
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "prometheus_k8s_metrics_api_minikube.json"
NAMESPACE = "monitoring"
PODS = ("prometheus-k8s-0", "prometheus-k8s-1")
TARGET = (NAMESPACE, "StatefulSet", "prometheus-k8s")
PROMETHEUS_REQUEST = 400 * MIB
RELOADER_REQUEST = 50 * MIB
#: Metrics API working set of config-reloader in the recorded snapshot (~15Mi).
RELOADER_WORKING_SET = 15680 * 1024
STEP = 30
SCAN_INTERVAL = 60
#: The pod cgroup series of the recording read 0 from this time on (cAdvisor gap).
CADVISOR_GAP = datetime(2026, 11, 8, 14, 0, 30, tzinfo=timezone.utc).timestamp()


def _fixture() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _by_pod(series: List[Dict[str, Any]]) -> Dict[str, Dict[float, float]]:
    return {item["metric"]["pod"]: {float(ts): float(value) for ts, value in item["values"]} for item in series}


class _ReplayClock(datetime):
    """The collector's clock during the replay: each scan runs "now" (injected, never the wall clock)."""

    current: datetime = datetime(2026, 11, 8, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current

    @classmethod
    def fromtimestamp(cls, t, tz=None):  # type: ignore[override]
        return datetime.fromtimestamp(t, tz=tz)


class _MetricsApi(MetricsServerCollector):
    """Metrics API samples of one scan: what metrics-server reported for each container at ``now``."""

    def __init__(self) -> None:
        super().__init__()
        self.samples: List[ContainerResourceUsage] = []

    async def collect(self) -> List[ContainerResourceUsage]:
        return list(self.samples)


def _requests(fixture: Dict[str, Any]) -> Dict[tuple, int]:
    return {
        (item["metric"]["pod"], item["metric"]["container"]): int(float(item["value"]))
        for item in fixture["range"]["memory_requests"]
    }


def _pod_metrics(fixture: Dict[str, Any]) -> List[PodMetric]:
    requests = _requests(fixture)
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


def _recorded(values: Dict[float, float], *, cpu: bool = False) -> Any:
    """Recorded value at a range timestamp (a constant CPU rate while the pod ran); None when absent."""

    def at(ts: float) -> Optional[float]:
        value = values.get(float(ts))
        if cpu:
            return 0.025 if value is not None else None
        return value

    return at


def _prometheus(fixture: Dict[str, Any]) -> FakePrometheus:
    """The recorded shape: one pod cgroup series per pod (no ``container`` label)."""
    working_set = _by_pod(fixture["range"]["pod_working_set"])
    restarts = {
        (item["metric"]["pod"], item["metric"]["container"]): float(item["values"][-1][1])
        for item in fixture["range"]["restarts"]
    }
    prom = FakePrometheus()
    for pod in PODS:
        labels = {"namespace": NAMESPACE, "pod": pod, "node": "minikube", "id": f"/kubepods/burstable/{pod}"}
        values = working_set[pod]
        prom.add("container_memory_working_set_bytes", labels, _recorded(values))
        prom.add("container_cpu_usage_seconds_total", labels, _recorded(values, cpu=True))
        for container in ("prometheus", "config-reloader"):
            prom.add(
                "kube_pod_container_status_restarts_total",
                {"namespace": NAMESPACE, "pod": pod, "container": container},
                restarts[(pod, container)],
            )
    return prom


def _scan_times(fixture: Dict[str, Any], until: float) -> List[float]:
    """One scan per minute while Prometheus has samples, as the collector runs (RECOMMENDATION_SCAN_INTERVAL=1m)."""
    present = set(_by_pod(fixture["range"]["pod_working_set"])[PODS[0]])
    first = min(present)
    times = []
    t = first + SCAN_INTERVAL
    while t <= until:
        if t in present and t - STEP in present:
            times.append(t)
        t += SCAN_INTERVAL
    return times


def _metrics_api_samples(usage: Dict[str, Dict[float, float]], last: Dict[str, float], t: float):
    """Container samples metrics-server reports at ``t``: the pod usage (page cache included), split per container.

    During the cAdvisor gap the kubelet's CRI stats keep reporting: the last
    known usage is carried forward.
    """
    samples = []
    now = datetime.fromtimestamp(t, tz=timezone.utc)
    for pod in PODS:
        value = usage[pod].get(t)
        if value is not None and value > MIB:
            last[pod] = value
        pod_usage = last.get(pod)
        if pod_usage is None:
            continue
        for container, memory, cpu in (
            ("prometheus", int(pod_usage) - RELOADER_WORKING_SET, 12),
            ("config-reloader", RELOADER_WORKING_SET, 1),
        ):
            samples.append(
                ContainerResourceUsage(
                    namespace=NAMESPACE,
                    pod_name=pod,
                    container_name=container,
                    cpu_usage_millicores=cpu,
                    memory_usage_bytes=memory,
                    timestamp=now,
                    duration_seconds=60,
                )
            )
    return samples


@pytest.fixture
async def replay(tmp_path, monkeypatch):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "bug2_metrics_api.db"),
        PROMETHEUS_QUERY_RANGE_STEP="30s",
        KUBERNETES_METRICS_API_ENABLED=True,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    repo = SQLiteCombinedMetricsRepository(dbm)
    fixture = _fixture()
    monkeypatch.setattr(range_module, "datetime", _ReplayClock)

    async def run(until: float):
        processor = build_range_processor(
            cfg,
            repo,
            _prometheus(fixture),
            FakeOpenCost(pods=[(NAMESPACE, pod) for pod in PODS]),
            nodes={
                "minikube": NodeInfo(
                    name="minikube", instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=4
                )
            },
        )
        processor.pod_collector.collect.return_value = _pod_metrics(fixture)
        metrics_api = _MetricsApi()
        processor._metrics_server_collector = metrics_api
        usage = _by_pod(fixture["range"]["pod_usage"])
        last: Dict[str, float] = {}
        rows = []
        for t in _scan_times(fixture, until):
            _ReplayClock.current = datetime.fromtimestamp(t + 5, tz=timezone.utc)
            metrics_api.samples = _metrics_api_samples(usage, last, t)
            scan_rows = await processor.run_range(
                datetime.fromtimestamp(t - SCAN_INTERVAL, tz=timezone.utc), datetime.fromtimestamp(t, tz=timezone.utc)
            )
            await repo.write_combined_metrics(scan_rows)
            rows.extend(scan_rows)
        end = datetime.fromtimestamp(until, tz=timezone.utc)
        return rows, end

    try:
        yield cfg, dbm, run
    finally:
        await dbm.close()


async def _snapshots(dbm, rows, end: datetime):
    reference = summarize_rows(
        [row for row in rows if not row.container_name], [row for row in rows if row.container_name], step_seconds=STEP
    )
    usage_repo = create_workload_usage_repository(dbm, "sqlite")
    await WorkloadUsageRollup(usage_repo, step_seconds=STEP, backfill_hours=48).refresh(end)
    sql = await usage_repo.read_usage_snapshot(end - timedelta(days=1), end + timedelta(hours=1))
    return {"python": reference, "sqlite": sql}


def _increase(cfg: Config, rows, usage) -> Recommendation:
    pods = [row for row in rows if not row.container_name]
    containers = [row for row in rows if row.container_name]
    context = OptimizationContext(config=cfg, metrics=pods, container_metrics=containers, usage=usage)
    recs = MemoryRightsizingAnalyzer(cfg).analyze(context)
    increases = [
        rec for rec in recs if (rec.recommended_memory_request_bytes or 0) > (rec.current_memory_request_bytes or 0)
    ]
    # One target for both replicas (never one Pod target per replica), one container.
    assert [(rec.namespace, rec.pod_name, rec.container_name) for rec in increases] == [
        (NAMESPACE, "prometheus-k8s", "prometheus")
    ]
    return increases[0]


def test_fixture_records_a_container_working_set_above_its_pod():
    """The recorded kubelet snapshot: the container figure contradicts the pod cgroup that contains it."""
    snapshot = _fixture()["kubelet_snapshot"]
    for pod in PODS:
        summary = snapshot["stats_summary"][pod]
        containers = sum(item["working_set_bytes"] for item in summary["containers"].values())
        assert containers > summary["pod_working_set_bytes"] * 1.2
        resource = snapshot["metrics_resource"]
        assert (
            resource[f"container_memory_working_set_bytes:{pod}/prometheus"]
            > resource[f"pod_memory_working_set_bytes:{pod}"] * 1.2
        )


@pytest.mark.asyncio
async def test_metrics_api_usage_is_refitted_to_the_pod_cgroup_and_sizes_a_sane_increase(replay):
    cfg, dbm, run = replay
    rows, end = await run(CADVISOR_GAP - STEP)

    pods = {(row.pod_name, row.timestamp): row for row in rows if not row.container_name}
    containers = [row for row in rows if row.container_name == "prometheus"]
    assert len(containers) > 400
    for row in containers:
        pod = pods[(row.pod_name, row.timestamp)]
        assert pod.memory_usage_bytes and row.memory_usage_bytes
        # A container never exceeds the pod cgroup that contains it.
        assert row.memory_usage_bytes <= pod.memory_usage_bytes
        assert "container_usage_exceeds_pod" in row.estimation_reasons and row.is_estimated

    for name, snapshot in (await _snapshots(dbm, rows, end)).items():
        summary = snapshot.summaries[TARGET]["prometheus"]
        assert summary.memory is not None, name
        # The real working set (~620Mi), not the page-cache-inflated Metrics API figure (~940Mi).
        assert 580 * MIB < summary.memory.p99 < 650 * MIB, name

        rec = _increase(cfg, rows, snapshot)
        assert rec.current_memory_request_bytes == PROMETHEUS_REQUEST
        recommended = rec.recommended_memory_request_bytes or 0
        # p99 (~620Mi) x 1.2 headroom, below the 2x step cap (800Mi): the cap does not decide.
        assert 700 * MIB <= recommended < 790 * MIB, (name, recommended / MIB)
        assert "Capped" not in rec.reason and "usage suggests" not in rec.reason
        assert "per-replica p99" in rec.reason
        assert rec.potential_savings_cost is not None and rec.potential_savings_cost <= 0


@pytest.mark.asyncio
async def test_unverifiable_metrics_api_usage_never_sizes_above_the_pod(replay):
    """During the cAdvisor gap the pod cgroup reads 0: the Metrics API figure cannot be checked at collection.

    Those samples keep the inflated value; the analyzer bounds the container's
    statistics by its pod-level summary and says so.
    """
    cfg, dbm, run = replay
    gap_end = datetime(2026, 11, 8, 14, 5, tzinfo=timezone.utc).timestamp()
    rows, end = await run(gap_end)

    pods = [row for row in rows if not row.container_name]
    # A zero pod cgroup working set is a cAdvisor gap, never a measurement.
    assert all(row.memory_usage_bytes is None or row.memory_usage_bytes > 300 * MIB for row in pods)
    unverified = [
        row
        for row in rows
        if row.container_name == "prometheus" and "container_usage_exceeds_pod" not in row.estimation_reasons
    ]
    assert unverified and max(row.memory_usage_bytes or 0 for row in unverified) > 850 * MIB

    for name, snapshot in (await _snapshots(dbm, rows, end)).items():
        summary = snapshot.summaries[TARGET]["prometheus"]
        assert summary.memory is not None and summary.memory.p99 > 850 * MIB, name

        rec = _increase(cfg, rows, snapshot)
        recommended = rec.recommended_memory_request_bytes or 0
        assert 700 * MIB <= recommended < 790 * MIB, (name, recommended / MIB)
        assert "container_usage_exceeds_pod" in rec.reason
        assert "Capped" not in rec.reason
