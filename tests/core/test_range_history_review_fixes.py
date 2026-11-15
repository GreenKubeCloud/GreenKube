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


# --- Second review -----------------------------------------------------------------------------------------------

RANGE = (BASE, BASE + timedelta(seconds=STEP))


async def _stored_containers(repo, pod="p"):
    return sorted(row.container_name for row in await repo.read_container_metrics(*RANGE) if row.pod_name == pod)


@pytest.mark.asyncio
async def test_recollection_from_a_stored_row_keeps_its_container_rows_despite_an_init_container(storage):
    """Second review 1a: the pod request includes an init container that has no container row."""
    cfg, repo, snapshots = storage
    live = [_pod(300), _pod(200, container="init-db").model_copy(update={"kind": "init"}), _pod(100, container="q")]
    live[2] = live[2].model_copy(update={"pod_name": "q", "container_name": "app"})
    first = await _build(cfg, repo, snapshots, pod_list=live).run_range(*RANGE)
    await repo.write_combined_metrics(first, replace_containers=True)
    assert await _stored_containers(repo) == ["app"]

    second = await _build(cfg, repo, None, pod_list=[]).run_range(*RANGE)
    recollected = _pod_row(second)
    await repo.write_combined_metrics(second, replace_containers=True)

    assert recollected.metadata_source == "stored_row"
    assert recollected.cpu_request == _pod_row(first).cpu_request
    assert await _stored_containers(repo) == ["app"]


@pytest.mark.asyncio
async def test_recollection_without_container_usage_keeps_the_stored_container_rows(storage):
    """Second review 1b: one container series is missing in Prometheus, so no container row is built."""
    cfg, repo, snapshots = storage
    live = [_pod(300), _pod(200, container="side"), _pod(100).model_copy(update={"pod_name": "q"})]
    first = await _build(cfg, repo, snapshots, pod_list=live, containers=("app", "side")).run_range(*RANGE)
    await repo.write_combined_metrics(first, replace_containers=True)

    second = await _build(cfg, repo, snapshots, pod_list=live, containers=("app",)).run_range(*RANGE)
    await repo.write_combined_metrics(second, replace_containers=True)

    assert await _stored_containers(repo) == ["app", "side"]


@pytest.mark.asyncio
async def test_rows_without_provenance_are_not_reused_as_stored_metadata(storage):
    """Second review 2: rows written before range.4 (metadata_source NULL) carry the collection-time state."""
    cfg, repo, snapshots = storage
    first = await _build(
        cfg, repo, snapshots, pod_list=[_pod(500), _pod(100).model_copy(update={"pod_name": "q"})]
    ).run_range(*RANGE)
    legacy = [row.model_copy(update={"metadata_source": None, "node_source": None}) for row in first]
    await repo.write_combined_metrics(legacy)

    second = await _build(cfg, repo, None, pod_list=[]).run_range(*RANGE)

    assert _pod_row(second).metadata_source == "missing"


def _ksm(prom: FakePrometheus, cpu: float = 0.5) -> FakePrometheus:
    base = {"namespace": "ns", "pod": "p"}
    prom.add("kube_pod_container_resource_requests", {**base, "container": "app", "resource": "cpu"}, cpu)
    prom.add("kube_pod_container_info", {**base, "container": "app"}, 1.0)
    prom.add(
        "kube_pod_owner", {**base, "owner_kind": "StatefulSet", "owner_name": "db", "owner_is_controller": "true"}, 1.0
    )
    return prom


@pytest.mark.asyncio
async def test_sample_before_the_first_observation_is_flagged(storage):
    """Second review 3: first observed 3 hours after the sample, a StatefulSet pod may have had another spec."""
    cfg, repo, snapshots = storage
    await snapshots.save_pods([_pod(900, uid="uid-2")], BASE + timedelta(hours=3))

    rows = await _build(cfg, repo, snapshots, pod_list=[]).run_range(*RANGE)

    row = _pod_row(rows)
    assert row.is_estimated
    assert "pod_metadata_uncertain" in row.estimation_reasons


@pytest.mark.asyncio
async def test_sample_before_the_first_observation_prefers_kube_state_metrics(storage):
    cfg, repo, snapshots = storage
    await snapshots.save_pods([_pod(900, uid="uid-2")], BASE + timedelta(hours=3))
    processor = _build(cfg, repo, snapshots, pod_list=[])
    _ksm(processor.prometheus_collector)

    row = _pod_row(await processor.run_range(*RANGE))

    assert (row.metadata_source, row.cpu_request) == ("kube_state_metrics", 500)
    assert "pod_metadata_uncertain" not in row.estimation_reasons


@pytest.mark.asyncio
async def test_kube_state_metrics_queries_are_filtered_and_include_sidecars_and_containers_without_requests(storage):
    """Second review 4."""
    cfg, repo, snapshots = storage
    processor = _build(cfg, repo, snapshots, pod_list=[], containers=("app", "side", "bare"))
    prom = _ksm(processor.prometheus_collector)
    base = {"namespace": "ns", "pod": "p"}
    prom.add("kube_pod_init_container_resource_requests", {**base, "container": "side", "resource": "cpu"}, 0.2)
    prom.add("kube_pod_init_container_info", {**base, "container": "side", "restart_policy": "Always"}, 1.0)
    prom.add("kube_pod_init_container_resource_requests", {**base, "container": "init", "resource": "cpu"}, 0.9)
    prom.add("kube_pod_init_container_info", {**base, "container": "init", "restart_policy": "Never"}, 1.0)
    prom.add("kube_pod_container_info", {**base, "container": "bare"}, 1.0)
    prom.add("kube_pod_container_info", {"namespace": "ns", "pod": "q", "container": "app"}, 1.0)
    reads = []
    original = repo.read_combined_metrics

    async def spy(*args, **kwargs):
        reads.append(args)
        return await original(*args, **kwargs)

    repo.read_combined_metrics = spy  # type: ignore[method-assign]

    rows = await processor.run_range(*RANGE)

    row = _pod_row(rows)
    assert (row.metadata_source, row.cpu_request) == ("kube_state_metrics", 700)
    containers = sorted((r.container_name, r.cpu_request) for r in rows if r.pod_name == "p" and r.container_name)
    assert containers == [("app", 500), ("bare", 0), ("side", 200)]
    ksm_queries = [query for query, *_rest in prom.queries if "kube_pod_container_resource" in query]
    assert ksm_queries and all('pod=~"' in query for query in ksm_queries)
    assert reads == []
