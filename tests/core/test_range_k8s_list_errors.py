"""WP-01.2 / COL-07: a failed Kubernetes pod or node list fails the window instead of storing degraded rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.node_collector import NodeCollector
from greenkube.collectors.pod_collector import PodCollector
from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.historical_range_processor import RangeCollectionError
from greenkube.models.metrics import PodMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.sqlite.pod_snapshot_repository import SQLitePodSnapshotRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


class _FailingApi:
    async def list_pod_for_all_namespaces(self, **_kwargs):
        raise TimeoutError("API server timeout")

    async def list_node(self, **_kwargs):
        raise TimeoutError("API server timeout")


def _failing_pod_collector() -> PodCollector:
    collector = PodCollector()
    collector._api = _FailingApi()
    return collector


def _failing_node_collector() -> NodeCollector:
    collector = NodeCollector()
    collector._api = _FailingApi()
    return collector


@pytest.fixture
async def storage(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    try:
        yield cfg, SQLitePodSnapshotRepository(dbm)
    finally:
        await dbm.close()


def _prometheus() -> FakePrometheus:
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.2)
    return prom


@pytest.mark.asyncio
async def test_collectors_keep_their_lenient_default():
    """Callers outside the range pipeline still get an empty result (unchanged contract)."""
    assert await _failing_pod_collector().collect() == []
    assert await _failing_node_collector().collect() == {}


@pytest.mark.asyncio
async def test_failed_pod_list_without_recent_history_fails_the_window(storage):
    """COL-07: the window is retried instead of being stored with request 0 and no owner."""
    cfg, snapshots = storage
    processor = build_range_processor(cfg, None, _prometheus(), FakeOpenCost(), pod_snapshot_repository=snapshots)
    processor.pod_collector = _failing_pod_collector()

    with pytest.raises(RangeCollectionError, match="pod list"):
        await processor.run_range(BASE, BASE + timedelta(seconds=STEP))


@pytest.mark.asyncio
async def test_failed_pod_list_with_recent_history_uses_the_history(storage):
    """COL-07: with a snapshot newer than one step the window proceeds from the history."""
    cfg, snapshots = storage
    pod = PodMetric(
        pod_name="p",
        namespace="ns",
        container_name="app",
        cpu_request=500,
        memory_request=0,
        owner_kind="Deployment",
        owner_name="d",
    )
    await snapshots.save_pods([pod], datetime.now(timezone.utc) - timedelta(seconds=60))
    processor = build_range_processor(cfg, None, _prometheus(), FakeOpenCost(), pod_snapshot_repository=snapshots)
    processor.pod_collector = _failing_pod_collector()

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert (row.owner_name, row.cpu_request, row.metadata_source) == ("d", 500, "k8s_snapshot")


@pytest.mark.asyncio
async def test_failed_node_list_without_node_history_fails_the_window(storage):
    """COL-07: without any node snapshot, a failed node list must not yield default zone/PUE rows."""
    cfg, snapshots = storage
    processor = build_range_processor(cfg, None, _prometheus(), FakeOpenCost(), pod_snapshot_repository=snapshots)
    processor.node_collector = _failing_node_collector()

    with pytest.raises(RangeCollectionError, match="node list"):
        await processor.run_range(BASE, BASE + timedelta(seconds=STEP))


@pytest.mark.asyncio
async def test_failed_node_list_with_node_history_proceeds_from_snapshots(storage):
    cfg, snapshots = storage
    snapshot = NodeInfo(name="n1", instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=2)
    snapshot.timestamp = BASE - timedelta(hours=1)
    processor = build_range_processor(
        cfg,
        None,
        _prometheus(),
        FakeOpenCost(),
        pod_snapshot_repository=snapshots,
        node_snapshots=[snapshot],
    )
    processor.node_collector = _failing_node_collector()

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.node_source == "snapshot"
    assert row.node_instance_type == "m5.large"


@pytest.mark.asyncio
async def test_persistent_pod_list_failure_stores_degraded_rows_after_the_failure_budget(storage):
    """Review 5: after COLLECTION_K8S_LIST_MAX_FAILURES attempts the window is stored, flagged, not left as a gap."""
    cfg, snapshots = storage
    cfg = cfg.model_copy(update={"COLLECTION_K8S_LIST_MAX_FAILURES": 2})
    processor = build_range_processor(cfg, None, _prometheus(), FakeOpenCost(), pod_snapshot_repository=snapshots)
    processor.pod_collector = _failing_pod_collector()
    before = _degraded("pods")

    with pytest.raises(RangeCollectionError, match="pod list"):
        await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.metadata_source == "missing"
    assert row.is_estimated
    assert "k8s_list_failed" in row.estimation_reasons
    assert _degraded("pods") == before + 1


@pytest.mark.asyncio
async def test_persistent_node_list_failure_stores_degraded_rows_after_the_failure_budget(storage):
    cfg, snapshots = storage
    cfg = cfg.model_copy(update={"COLLECTION_K8S_LIST_MAX_FAILURES": 1})
    processor = build_range_processor(cfg, None, _prometheus(), FakeOpenCost(), pod_snapshot_repository=snapshots)
    processor.node_collector = _failing_node_collector()

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.node_source == "unknown"
    assert "k8s_list_failed" in row.estimation_reasons


def _degraded(resource: str) -> float:
    from greenkube.core.collection_telemetry import DEGRADED_WINDOWS

    return DEGRADED_WINDOWS.labels(list=resource)._value.get()
