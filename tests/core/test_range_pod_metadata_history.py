"""WP-01.1 / COL-01: pod requests, owner and containers come from the pod history at sample time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import PodMetric
from greenkube.storage.sqlite.pod_snapshot_repository import SQLitePodSnapshotRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
POD_P = PodMetric(
    pod_name="p",
    namespace="ns",
    container_name="app",
    cpu_request=500,
    memory_request=256 * 1024**2,
    owner_kind="Deployment",
    owner_name="d",
)


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
        yield cfg, SQLiteCombinedMetricsRepository(dbm), SQLitePodSnapshotRepository(dbm)
    finally:
        await dbm.close()


def _prometheus() -> FakePrometheus:
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.2)
    prom.add("container_memory_working_set_bytes", labels, 100 * 1024**2)
    return prom


def _build(cfg, repo, snapshots, pod_list):
    return build_range_processor(
        cfg,
        repo,
        _prometheus(),
        FakeOpenCost(),
        pod_list=pod_list,
        pod_snapshot_repository=snapshots,
    )


@pytest.mark.asyncio
async def test_deleted_pod_takes_owner_requests_and_containers_from_snapshot(storage):
    """COL-01 a: P is absent from the pod list at collection time but has a snapshot."""
    cfg, repo, snapshots = storage
    await snapshots.save_pods([POD_P], BASE + timedelta(minutes=2))
    processor = _build(cfg, repo, snapshots, pod_list=[])

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=2 * STEP))

    pods = [row for row in rows if not row.container_name]
    containers = [row for row in rows if row.container_name]
    assert len(pods) == 2
    for row in pods:
        assert (row.owner_kind, row.owner_name) == ("Deployment", "d")
        assert row.cpu_request == 500
        assert row.memory_request == 256 * 1024**2
        assert row.metadata_source == "k8s_snapshot"
        assert "pod_metadata_missing" not in row.estimation_reasons
    assert len(containers) == 2
    assert all(row.owner_name == "d" and row.cpu_request == 500 for row in containers)


@pytest.mark.asyncio
async def test_live_pod_list_is_recorded_in_the_history(storage):
    """Every collection records the live pod list so a later re-collection can use it."""
    cfg, repo, snapshots = storage
    processor = _build(cfg, repo, snapshots, pod_list=[POD_P])

    await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    stored = await snapshots.get_snapshots(BASE - timedelta(days=30), datetime.now(timezone.utc) + timedelta(hours=1))
    assert [version.owner_name for version in stored[("ns", "p")]] == ["d"]


@pytest.mark.asyncio
async def test_pod_without_any_metadata_is_flagged_missing(storage):
    """COL-01 b (partial): no snapshot and no live pod -> metadata_source missing, flagged."""
    cfg, repo, snapshots = storage
    processor = _build(cfg, repo, snapshots, pod_list=[])

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.metadata_source == "missing"
    assert row.owner_kind is None
    assert row.is_estimated
    assert "pod_metadata_missing" in row.estimation_reasons


@pytest.mark.asyncio
async def test_recollection_without_metadata_keeps_stored_owner_and_requests(storage):
    """COL-01 d: re-running a window after the pod is gone never erases stored owner/request."""
    cfg, repo, snapshots = storage
    first = await _build(cfg, repo, snapshots, pod_list=[POD_P]).run_range(BASE, BASE + timedelta(seconds=STEP))
    await repo.write_combined_metrics(first)

    # The history is lost too (worst case): the re-collected row reuses the stored row's metadata (review 4).
    second = await _build(cfg, repo, None, pod_list=[]).run_range(BASE, BASE + timedelta(seconds=STEP))
    assert next(row for row in second if not row.container_name).metadata_source == "stored_row"
    await repo.write_combined_metrics(second)

    stored = [row for row in await repo.read_combined_metrics(BASE, BASE + timedelta(seconds=STEP))]
    assert len(stored) == 1
    assert (stored[0].owner_kind, stored[0].owner_name) == ("Deployment", "d")
    assert stored[0].cpu_request == 500
    assert stored[0].metadata_source == "stored_row"


@pytest.mark.asyncio
async def test_collection_prunes_pod_history_older_than_node_data_max_age(storage):
    cfg, repo, snapshots = storage
    old = PodMetric(pod_name="old", namespace="ns", container_name="app", cpu_request=1, memory_request=1)
    await snapshots.save_pods([old], datetime.now(timezone.utc) - timedelta(days=cfg.NODE_DATA_MAX_AGE_DAYS + 1))

    await _build(cfg, repo, snapshots, pod_list=[POD_P]).run_range(BASE, BASE + timedelta(seconds=STEP))

    stored = await snapshots.get_snapshots(datetime(2000, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
    assert ("ns", "old") not in stored
