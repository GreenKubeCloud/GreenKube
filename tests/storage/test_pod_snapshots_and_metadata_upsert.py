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
from greenkube.storage.postgres.pod_snapshot_repository import PostgresPodSnapshotRepository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
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
    assert stored.node_source == "unknown"
    assert stored.joules == 12.0

    # A row with metadata still replaces it (an in-place resize is a real change).
    await repo.write_combined_metrics([_row(cpu_request=700)])
    (stored,) = await repo.read_combined_metrics(BASE, BASE + timedelta(minutes=5))
    assert stored.cpu_request == 700
