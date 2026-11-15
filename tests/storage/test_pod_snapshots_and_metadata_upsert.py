"""Pod metadata history and the metadata-preserving upsert (WP-01.1, COL-01) on SQLite and PostgreSQL."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import CombinedMetric, PodMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.base_pod_snapshot_repository import snapshot_at
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.postgres.pod_snapshot_repository import PostgresPodSnapshotRepository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
from greenkube.storage.sqlite.pod_snapshot_repository import SQLitePodSnapshotRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

BASE = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[tuple[str, DatabaseManager], None]:
    backend = request.param
    dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL pod snapshot tests.")
        schema = f"gk_pods_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "pods.db"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield backend, manager
    finally:
        await manager.close()
        if dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _pod(cpu: int) -> PodMetric:
    return PodMetric(
        pod_name="p",
        namespace="ns",
        container_name="app",
        cpu_request=cpu,
        memory_request=1024,
        cpu_limit=None,
        owner_kind="Deployment",
        owner_name="d",
    )


@pytest.mark.asyncio
async def test_pod_snapshots_keep_one_version_per_spec_with_its_interval(database):
    backend, manager = database
    repo = SQLitePodSnapshotRepository(manager) if backend == "sqlite" else PostgresPodSnapshotRepository(manager)

    await repo.save_pods([_pod(500)], BASE)
    await repo.save_pods([_pod(500)], BASE + timedelta(minutes=5))
    await repo.save_pods([_pod(700)], BASE + timedelta(minutes=10))

    versions = (await repo.get_snapshots(BASE - timedelta(hours=1), BASE + timedelta(hours=1)))[("ns", "p")]
    assert [(v.first_seen, v.last_seen) for v in versions] == [
        (BASE, BASE + timedelta(minutes=5)),
        (BASE + timedelta(minutes=10), BASE + timedelta(minutes=10)),
    ]
    assert [v.pod_metrics()[0].cpu_request for v in versions] == [500, 700]
    assert versions[0].pod_metrics()[0].owner_name == "d"
    assert await repo.get_snapshots(BASE + timedelta(hours=1), BASE + timedelta(hours=2)) == {}


def _row(**overrides) -> CombinedMetric:
    values = {
        "pod_name": "p",
        "namespace": "ns",
        "timestamp": BASE,
        "duration_seconds": 300,
        "cpu_request": 500,
        "memory_request": 1024,
        "owner_kind": "Deployment",
        "owner_name": "d",
        "joules": 10.0,
        "metadata_source": "k8s_snapshot",
        "node_source": "snapshot",
    }
    values.update(overrides)
    return CombinedMetric(**values)


@pytest.mark.asyncio
async def test_row_without_metadata_never_erases_stored_owner_and_requests(database):
    backend, manager = database
    repo = (
        SQLiteCombinedMetricsRepository(manager) if backend == "sqlite" else PostgresCombinedMetricsRepository(manager)
    )
    await repo.write_combined_metrics([_row()])
    await repo.write_combined_metrics(
        [
            _row(
                cpu_request=0,
                memory_request=0,
                owner_kind=None,
                owner_name=None,
                joules=12.0,
                metadata_source="missing",
                node_source="unknown",
            )
        ]
    )
    (stored,) = await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5))
    assert (stored.owner_kind, stored.owner_name, stored.cpu_request, stored.memory_request) == (
        "Deployment",
        "d",
        500,
        1024,
    )
    assert stored.metadata_source == "k8s_snapshot"
    # The whole stored row is kept (review 4): its energy was computed with these requests.
    assert stored.node_source == "snapshot"
    assert stored.joules == 10.0

    # A row with metadata still replaces it (an in-place resize is a real change).
    await repo.write_combined_metrics([_row(cpu_request=700)])
    (stored,) = await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5))
    assert stored.cpu_request == 700


@pytest.mark.asyncio
async def test_pod_snapshots_older_than_the_retention_are_pruned(database):
    backend, manager = database
    repo = SQLitePodSnapshotRepository(manager) if backend == "sqlite" else PostgresPodSnapshotRepository(manager)
    old = PodMetric(pod_name="old", namespace="ns", container_name="app", cpu_request=1, memory_request=1)
    await repo.save_pods([old], BASE - timedelta(days=40))
    await repo.save_pods([_pod(500)], BASE)

    assert await repo.prune(BASE - timedelta(days=30)) == 1

    remaining = await repo.get_snapshots(BASE - timedelta(days=60), BASE + timedelta(hours=1))
    assert list(remaining) == [("ns", "p")]


def _snapshot_repo(backend, manager):
    return SQLitePodSnapshotRepository(manager) if backend == "sqlite" else PostgresPodSnapshotRepository(manager)


def _metrics_repo(backend, manager):
    return (
        SQLiteCombinedMetricsRepository(manager) if backend == "sqlite" else PostgresCombinedMetricsRepository(manager)
    )


@pytest.mark.asyncio
async def test_rolled_back_spec_starts_a_new_version(database):
    """Review 3: A -> B -> A must resolve to A after the rollback, not to B."""
    backend, manager = database
    repo = _snapshot_repo(backend, manager)
    await repo.save_pods([_pod(100)], BASE)
    await repo.save_pods([_pod(200)], BASE + timedelta(hours=1))
    await repo.save_pods([_pod(100)], BASE + timedelta(hours=2))

    versions = (await repo.get_snapshots(BASE - timedelta(hours=1), BASE + timedelta(hours=4)))[("ns", "p")]
    assert [v.pod_metrics()[0].cpu_request for v in versions] == [100, 200, 100]
    later = snapshot_at(versions, BASE + timedelta(hours=3))
    between = snapshot_at(versions, BASE + timedelta(minutes=90))
    assert later is not None and later.pod_metrics()[0].cpu_request == 100
    assert between is not None and between.pod_metrics()[0].cpu_request == 200


@pytest.mark.asyncio
async def test_pod_versions_are_found_without_a_lower_bound(database):
    """Review 2: a pod last seen long before the window still has its history."""
    backend, manager = database
    repo = _snapshot_repo(backend, manager)
    await repo.save_pods([_pod(100)], BASE - timedelta(hours=6))

    found = await repo.get_pod_versions([("ns", "p"), ("ns", "absent")], BASE + timedelta(hours=1))
    assert list(found) == [("ns", "p")]
    assert found[("ns", "p")][0].pod_metrics()[0].cpu_request == 100
    assert await repo.get_pod_versions([("ns", "p")], BASE - timedelta(hours=7)) == {}


@pytest.mark.asyncio
async def test_pod_uid_change_starts_a_new_version(database):
    """Review 6: a recreated pod (new UID) is a new version even with the same spec; uid and cluster are stored."""
    backend, manager = database
    repo = _snapshot_repo(backend, manager)
    first = _pod(100).model_copy(update={"pod_uid": "uid-1"})
    second = _pod(100).model_copy(update={"pod_uid": "uid-2"})
    await repo.save_pods([first], BASE, cluster_uid="c1")
    await repo.save_pods([first], BASE + timedelta(minutes=5), cluster_uid="c1")
    await repo.save_pods([second], BASE + timedelta(hours=1), cluster_uid="c1")

    versions = (await repo.get_snapshots(BASE - timedelta(hours=1), BASE + timedelta(hours=2)))[("ns", "p")]
    assert [(v.pod_uid, v.first_seen, v.last_seen) for v in versions] == [
        ("uid-1", BASE, BASE + timedelta(minutes=5)),
        ("uid-2", BASE + timedelta(hours=1), BASE + timedelta(hours=1)),
    ]
    assert all(v.cluster_uid == "c1" for v in versions)


def _container_row(container: str, joules: float, **overrides) -> CombinedMetric:
    return _row(container_name=container, joules=joules, **overrides)


@pytest.mark.asyncio
async def test_row_without_metadata_keeps_the_whole_stored_row_and_its_containers(database):
    """Review 4: a 'missing' re-collection never mixes stored requests with values computed from request 0."""
    backend, manager = database
    repo = _metrics_repo(backend, manager)
    await repo.write_combined_metrics([_row(), _container_row("app", 6.0), _container_row("side", 4.0)])
    await repo.write_combined_metrics(
        [
            _row(
                cpu_request=0,
                memory_request=0,
                owner_kind=None,
                owner_name=None,
                joules=12.0,
                is_estimated=True,
                estimation_reasons=["pod_metadata_missing"],
                metadata_source="missing",
                node_source="unknown",
            )
        ]
    )
    (stored,) = await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5))
    assert (stored.cpu_request, stored.joules, stored.metadata_source, stored.node_source) == (
        500,
        10.0,
        "k8s_snapshot",
        "snapshot",
    )
    assert "pod_metadata_missing" not in stored.estimation_reasons
    containers = await repo.read_container_metrics(BASE, BASE + timedelta(minutes=5))
    assert sorted((c.container_name, c.joules) for c in containers) == [("app", 6.0), ("side", 4.0)]


@pytest.mark.asyncio
async def test_rewritten_pod_row_replaces_its_container_rows(database):
    """Review 4 / WP-01.1 d: container rows of a re-collected pod row are rewritten with it."""
    backend, manager = database
    repo = _metrics_repo(backend, manager)
    await repo.write_combined_metrics([_row(), _container_row("app", 6.0), _container_row("old", 4.0)])
    # Pod rows written alone (recompute jobs) never touch container rows.
    await repo.write_combined_metrics([_row(joules=8.0)])
    assert len(await repo.read_container_metrics(BASE, BASE + timedelta(minutes=5))) == 2
    await repo.write_combined_metrics([_row(joules=8.0), _container_row("app", 8.0)], replace_containers=True)

    containers = await repo.read_container_metrics(BASE, BASE + timedelta(minutes=5))
    assert [(c.container_name, c.joules) for c in containers] == [("app", 8.0)]


@pytest.mark.asyncio
async def test_node_snapshot_records_when_it_was_last_confirmed(database):
    """Review 1: an unchanged node extends its SCD version so the age check uses the last confirmation."""
    backend, manager = database
    repo = SQLiteNodeRepository(manager) if backend == "sqlite" else PostgresNodeRepository(manager)
    node = NodeInfo(name="n1", instance_type="m5.large", cloud_provider="aws", zone="eu-west-3a")
    await repo.save_nodes([node.model_copy(update={"timestamp": BASE - timedelta(days=40)})])
    await repo.save_nodes([node.model_copy(update={"timestamp": BASE - timedelta(minutes=10)})])

    (latest,) = await repo.get_latest_snapshots_before(BASE)
    assert latest.timestamp == BASE - timedelta(days=40)
    assert latest.confirmed_until == BASE - timedelta(minutes=10)


@pytest.mark.asyncio
async def test_timeseries_reports_null_cpu_when_no_row_has_a_cpu_sample(database):
    """Review 7: a bucket whose rows all lack a CPU sample reports NULL usage, not 0."""
    backend, manager = database
    repo = _metrics_repo(backend, manager)
    await repo.write_combined_metrics([_row(cpu_usage_millicores=None, memory_usage_bytes=None)])

    (point,) = await repo.aggregate_timeseries(
        start_time=BASE - timedelta(hours=1), end_time=BASE + timedelta(hours=1), granularity="hour"
    )
    assert point["cpu_usage_millicores"] is None
    assert point["memory_usage_bytes"] is None


@pytest.mark.asyncio
async def test_pod_row_without_container_rows_keeps_the_stored_consistent_pair(database):
    """Third review F1: a pass without container rows must not rewrite the pod row under stale containers."""
    backend, manager = database
    repo = _metrics_repo(backend, manager)
    await repo.write_combined_metrics([_row(), _container_row("app", 6.0), _container_row("side", 4.0)])

    await repo.write_combined_metrics([_row(joules=8.0)], replace_containers=True)

    (stored,) = await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5))
    containers = await repo.read_container_metrics(BASE, BASE + timedelta(minutes=5))
    assert stored.joules == pytest.approx(sum(c.joules for c in containers))
    assert stored.joules == 10.0
