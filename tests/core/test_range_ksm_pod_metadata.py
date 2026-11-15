"""WP-01.1 step 2 / COL-01: pod metadata from kube-state-metrics for pods never seen live."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.storage.sqlite.pod_snapshot_repository import SQLitePodSnapshotRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


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


def _prometheus(*, with_ksm: bool = True) -> FakePrometheus:
    prom = FakePrometheus()
    for container in ("app", "side"):
        labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": container}
        prom.add("container_cpu_usage_seconds_total", labels, 0.2)
        prom.add("container_memory_working_set_bytes", labels, 100 * 1024**2)
    if with_ksm:
        base = {"namespace": "ns", "pod": "p"}
        # Request changes at 10:05 (in-place resize): KSM is exact per step.
        prom.add(
            "kube_pod_container_resource_requests",
            {**base, "container": "app", "resource": "cpu", "unit": "core"},
            # Evaluated at the end of each step: the 10:05 evaluation describes [10:00, 10:05).
            lambda ts: 0.5 if ts <= BASE.timestamp() + STEP else 0.7,
        )
        prom.add(
            "kube_pod_container_resource_requests",
            {**base, "container": "app", "resource": "memory", "unit": "byte"},
            256 * 1024**2,
        )
        prom.add(
            "kube_pod_container_resource_requests",
            {**base, "container": "side", "resource": "cpu", "unit": "core"},
            0.1,
        )
        prom.add(
            "kube_pod_container_resource_limits",
            {**base, "container": "app", "resource": "cpu", "unit": "core"},
            1.0,
        )
        prom.add(
            "kube_pod_owner",
            {**base, "owner_kind": "ReplicaSet", "owner_name": "web-7d9f8c6b5", "owner_is_controller": "true"},
            1.0,
        )
    return prom


def _build(cfg, repo, snapshots, prom):
    return build_range_processor(cfg, repo, prom, FakeOpenCost(), pod_list=[], pod_snapshot_repository=snapshots)


@pytest.mark.asyncio
async def test_pod_never_seen_live_takes_requests_owner_and_containers_from_kube_state_metrics(storage):
    cfg, repo, snapshots = storage

    rows = await _build(cfg, repo, snapshots, _prometheus()).run_range(BASE, BASE + timedelta(seconds=2 * STEP))

    pods = sorted((row for row in rows if not row.container_name), key=lambda row: row.timestamp)
    assert [row.metadata_source for row in pods] == ["kube_state_metrics", "kube_state_metrics"]
    assert [row.cpu_request for row in pods] == [600, 800]
    assert all((row.owner_kind, row.owner_name) == ("Deployment", "web") for row in pods)
    assert pods[0].memory_request == 256 * 1024**2
    assert all("pod_metadata_missing" not in row.estimation_reasons for row in pods)
    containers = [row for row in rows if row.container_name and row.timestamp == BASE]
    assert sorted((row.container_name, row.cpu_request) for row in containers) == [("app", 500), ("side", 100)]
    app = next(row for row in containers if row.container_name == "app")
    assert app.cpu_limit == 1000


@pytest.mark.asyncio
async def test_without_kube_state_metrics_the_pod_stays_missing(storage):
    cfg, repo, snapshots = storage

    rows = await _build(cfg, repo, snapshots, _prometheus(with_ksm=False)).run_range(
        BASE, BASE + timedelta(seconds=STEP)
    )

    row = next(row for row in rows if not row.container_name)
    assert row.metadata_source == "missing"
    assert "pod_metadata_missing" in row.estimation_reasons
