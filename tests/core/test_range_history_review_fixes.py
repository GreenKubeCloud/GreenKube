"""WP-01.1 review fixes: node and pod history resolved correctly at sample time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.models.metrics import PodMetric
from greenkube.storage.sqlite.pod_snapshot_repository import SQLitePodSnapshotRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]
from tests.core.test_range_state_at_sample_time import (  # pyrefly: ignore[missing-import]
    _prometheus as _gone_node_prometheus,
)
from tests.core.test_range_state_at_sample_time import _snapshot  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def _pod(cpu: int = 500, uid: str | None = None, container: str = "app") -> PodMetric:
    return PodMetric(
        pod_name="p",
        namespace="ns",
        container_name=container,
        cpu_request=cpu,
        memory_request=256 * 1024**2,
        owner_kind="StatefulSet",
        owner_name="db",
        pod_uid=uid,
    )


@pytest.fixture
async def storage(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
        DEFAULT_ZONE="unknown",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    try:
        yield cfg, SQLiteCombinedMetricsRepository(dbm), SQLitePodSnapshotRepository(dbm)
    finally:
        await dbm.close()


def _prometheus(containers=("app",)) -> FakePrometheus:
    prom = FakePrometheus()
    for container in containers:
        labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": container}
        prom.add("container_cpu_usage_seconds_total", labels, 0.2)
        prom.add("container_memory_working_set_bytes", labels, 100 * 1024**2)
    other = {"namespace": "ns", "pod": "q", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", other, 0.1)
    prom.add("container_memory_working_set_bytes", other, 50 * 1024**2)
    return prom


def _build(cfg, repo, snapshots, pod_list, containers=("app",)):
    return build_range_processor(
        cfg,
        repo,
        _prometheus(containers),
        FakeOpenCost(pods=(("ns", "p"), ("ns", "q"))),
        pods=(("ns", "q", "app"),),
        pod_list=pod_list,
        pod_snapshot_repository=snapshots,
    )


def _pod_row(rows):
    return next(row for row in rows if row.pod_name == "p" and not row.container_name)


@pytest.mark.asyncio
async def test_removed_node_confirmed_recently_keeps_its_snapshot_despite_an_old_valid_from(tmp_path):
    """Review 1: valid_from 40 days ago, confirmed unchanged 10 minutes before the sample."""
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "r.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
        DEFAULT_PUE=1.3,
    )
    snapshot = _snapshot(BASE - timedelta(days=40)).model_copy(update={"confirmed_until": BASE - timedelta(minutes=10)})
    processor = build_range_processor(
        cfg,
        MagicMock(),
        _gone_node_prometheus(),
        FakeOpenCost(),
        nodes={},
        instance_types={},
        node_snapshots=[snapshot],
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.node_source == "snapshot"
    assert row.emaps_zone == "FR"
    assert row.embodied_co2e_grams > 0


@pytest.mark.asyncio
async def test_pod_deleted_during_a_long_downtime_keeps_its_metadata(storage):
    """Review 2: the pod was last seen 3 hours before the window it still ran in."""
    cfg, repo, snapshots = storage
    await snapshots.save_pods([_pod()], BASE - timedelta(hours=3))

    rows = await _build(cfg, repo, snapshots, pod_list=[]).run_range(BASE, BASE + timedelta(seconds=STEP))

    row = _pod_row(rows)
    assert row.metadata_source == "k8s_snapshot"
    assert (row.owner_name, row.cpu_request) == ("db", 500)


@pytest.mark.asyncio
async def test_pod_recreated_with_a_new_uid_during_downtime_is_flagged(storage):
    """Review 6: a StatefulSet pod recreated with a new spec during the downtime."""
    cfg, repo, snapshots = storage
    await snapshots.save_pods([_pod(500, uid="uid-1")], BASE - timedelta(hours=3))
    await snapshots.save_pods([_pod(900, uid="uid-2")], BASE + timedelta(hours=3))

    rows = await _build(cfg, repo, snapshots, pod_list=[]).run_range(BASE, BASE + timedelta(seconds=STEP))

    row = _pod_row(rows)
    assert row.is_estimated
    assert "pod_metadata_uncertain" in row.estimation_reasons


@pytest.mark.asyncio
async def test_pod_with_the_same_uid_after_a_gap_is_not_flagged(storage):
    cfg, repo, snapshots = storage
    await snapshots.save_pods([_pod(500, uid="uid-1")], BASE - timedelta(hours=3))
    await snapshots.save_pods([_pod(900, uid="uid-1")], BASE + timedelta(hours=3))

    rows = await _build(cfg, repo, snapshots, pod_list=[]).run_range(BASE, BASE + timedelta(seconds=STEP))

    assert "pod_metadata_uncertain" not in _pod_row(rows).estimation_reasons


@pytest.mark.asyncio
async def test_recollection_without_metadata_reuses_stored_requests_and_rewrites_containers(storage):
    """Review 4 / WP-01.1 d: re-collecting after the pod and its history are gone keeps a consistent row."""
    cfg, repo, snapshots = storage
    containers = ("app", "side")
    live = [_pod(300, container="app"), _pod(200, container="side"), _pod(100).model_copy(update={"pod_name": "q"})]
    first = await _build(cfg, repo, snapshots, pod_list=live, containers=containers).run_range(
        BASE, BASE + timedelta(seconds=STEP)
    )
    await repo.write_combined_metrics(first)
    expected = _pod_row(first)

    second = await _build(cfg, repo, None, pod_list=[], containers=containers).run_range(
        BASE, BASE + timedelta(seconds=STEP)
    )
    recollected = _pod_row(second)
    assert recollected.cpu_request == 500
    assert recollected.joules == pytest.approx(expected.joules)
    assert recollected.co2e_grams == pytest.approx(expected.co2e_grams)
    assert recollected.total_cost == pytest.approx(expected.total_cost)
    assert "pod_metadata_missing" not in recollected.estimation_reasons
    await repo.write_combined_metrics(second, replace_containers=True)

    (stored,) = [
        row for row in await repo.read_combined_metrics(BASE, BASE + timedelta(seconds=STEP)) if row.pod_name == "p"
    ]
    stored_containers = [
        row for row in await repo.read_container_metrics(BASE, BASE + timedelta(seconds=STEP)) if row.pod_name == "p"
    ]
    assert stored.cpu_request == 500
    assert stored.joules == pytest.approx(expected.joules)
    assert sorted(row.container_name for row in stored_containers) == ["app", "side"]
    assert sum(row.joules for row in stored_containers) == pytest.approx(stored.joules)
