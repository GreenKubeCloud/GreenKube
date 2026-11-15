"""Stable installation identity for collection watermarks and records (COL-02, STO-09)."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.cluster_identity import (
    SOURCE_CLUSTER_NAME,
    SOURCE_KUBE_SYSTEM,
    ClusterIdentityMismatch,
    InstallationIdentityUnavailable,
    adopt_legacy_collection_keys,
    resolve_installation_identity,
)
from greenkube.core.collection import METRICS_WATERMARK_SOURCE, MetricsCollectionJob
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.storage.base_collection_watermark_repository import create_collection_watermark_repository
from greenkube.storage.base_collection_window_repository import (
    STATUS_COLLECTED,
    CollectionWindowRecord,
    create_collection_window_repository,
)
from greenkube.storage.installation_repository import create_installation_repository, is_cluster_uid
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

UID_A = "0f6b8a2e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
UID_B = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
STEP = timedelta(minutes=5)
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def _reader(value):
    async def read():
        return value

    return read


@pytest.fixture(params=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[tuple[str, DatabaseManager], None]:
    backend = request.param
    dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL installation identity tests.")
        schema = f"gk_id_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "identity.db"))
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


@pytest.mark.asyncio
async def test_identity_is_stored_once_and_survives_a_rename(database):
    backend, manager = database
    repo = create_installation_repository(manager, backend)

    first = await resolve_installation_identity(repo, "kind-worker", reader=_reader(UID_A))
    renamed = await resolve_installation_identity(repo, "prod-eu", reader=_reader(UID_A))

    assert (first.cluster_uid, first.source) == (UID_A, SOURCE_KUBE_SYSTEM)
    assert renamed.cluster_uid == UID_A


@pytest.mark.asyncio
async def test_a_controller_of_another_cluster_refuses_the_database(database):
    """STO-09 acceptance: a second cluster fails before any write, with an explicit message."""
    backend, manager = database
    repo = create_installation_repository(manager, backend)
    await resolve_installation_identity(repo, "dev", reader=_reader(UID_A))

    with pytest.raises(ClusterIdentityMismatch, match="one database schema per cluster"):
        await resolve_installation_identity(repo, "prod", reader=_reader(UID_B))

    adopted = await resolve_installation_identity(repo, "prod", reader=_reader(UID_B), allow_change=True)
    assert adopted.cluster_uid == UID_B


@pytest.mark.asyncio
async def test_name_identity_is_upgraded_to_the_kube_system_uid(database):
    backend, manager = database
    repo = create_installation_repository(manager, backend)

    local = await resolve_installation_identity(repo, "laptop", reader=_reader(None))
    upgraded = await resolve_installation_identity(repo, "laptop", reader=_reader(UID_A))

    assert (local.cluster_uid, local.source) == ("name:laptop", SOURCE_CLUSTER_NAME)
    assert upgraded.cluster_uid == UID_A


@pytest.mark.asyncio
async def test_unreadable_identity_is_retried_then_fails_closed():
    calls = []

    async def failing():
        calls.append(1)
        raise RuntimeError("API server not reachable yet")

    async def no_sleep(_seconds):
        return None

    with pytest.raises(InstallationIdentityUnavailable):
        await resolve_installation_identity(None, "x", reader=failing, attempts=3, sleep=no_sleep)  # type: ignore[arg-type]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_legacy_watermarks_and_records_are_adopted(database):
    """COL-02 step 4: node-name and default keys merge into the identity; the newest watermark wins."""
    backend, manager = database
    repo = create_installation_repository(manager, backend)
    watermarks = create_collection_watermark_repository(manager, backend)
    windows = create_collection_window_repository(manager, backend)
    await watermarks.advance("kind-worker", METRICS_WATERMARK_SOURCE, BASE)
    await watermarks.advance("kind-worker2", METRICS_WATERMARK_SOURCE, BASE + 3 * STEP)
    await watermarks.advance(UID_B, METRICS_WATERMARK_SOURCE, BASE + 9 * STEP)  # another installation's uid
    await windows.record(
        [
            CollectionWindowRecord("kind-worker", METRICS_WATERMARK_SOURCE, BASE, BASE + STEP, STATUS_COLLECTED, 1, 1),
            CollectionWindowRecord("kind-worker2", METRICS_WATERMARK_SOURCE, BASE, BASE + STEP, STATUS_COLLECTED, 1, 1),
        ]
    )

    adopted = await adopt_legacy_collection_keys(repo, UID_A, METRICS_WATERMARK_SOURCE)

    assert adopted.keys == ("kind-worker", "kind-worker2")
    assert [(w.cluster_uid, w.last_complete_end) for w in await watermarks.list_all()] == [
        (UID_A, BASE + 3 * STEP),
        (UID_B, BASE + 9 * STEP),
    ]
    records = await windows.list_overlapping(UID_A, METRICS_WATERMARK_SOURCE, BASE, BASE + STEP)
    assert len(records) == 1
    assert not await windows.has_records("kind-worker", METRICS_WATERMARK_SOURCE)
    assert (await adopt_legacy_collection_keys(repo, UID_A, METRICS_WATERMARK_SOURCE)).keys == ()


def test_uid_detection():
    assert is_cluster_uid(UID_A)
    assert not is_cluster_uid("kind-worker") and not is_cluster_uid("name:default")


@pytest.mark.asyncio
async def test_controllers_on_two_nodes_share_one_watermark(tmp_path):
    """COL-02 acceptance b): K8S_NODE_NAME a then b, one database: the second plans nothing collected."""
    cfg_a = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "shared.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        COLLECTION_SAFETY_LAG="1m",
        COLLECTION_MAX_BACKFILL="24h",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLUSTER_NAME="worker-a",
    )
    cfg_b = cfg_a.model_copy(update={"CLUSTER_NAME": "worker-b"})
    dbm = DatabaseManager(cfg_a)
    await dbm.connect()
    try:
        prom = FakePrometheus()
        prom.add(
            "container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}, 0.1
        )
        metrics = SQLiteCombinedMetricsRepository(dbm)
        processor = build_range_processor(cfg_a, metrics, prom, FakeOpenCost())
        repo = create_installation_repository(dbm, "sqlite")
        reports = []
        for cfg in (cfg_a, cfg_b):
            identity = await resolve_installation_identity(repo, cfg.CLUSTER_NAME, reader=_reader(UID_A))
            job = MetricsCollectionJob(
                processor=processor,
                metrics_repository=metrics,
                watermark_repository=create_collection_watermark_repository(dbm, "sqlite"),
                config=cfg,
                cluster_uid=identity.cluster_uid,
                clock=lambda: BASE + 2 * STEP + timedelta(minutes=1),
                window_repository=create_collection_window_repository(dbm, "sqlite"),
            )
            reports.append(await job.run())

        assert reports[0].windows_planned > 0
        assert reports[1].windows_planned == 0
        assert [w.cluster_uid for w in await create_collection_watermark_repository(dbm, "sqlite").list_all()] == [
            UID_A
        ]
    finally:
        await dbm.close()


@pytest.mark.asyncio
async def test_missing_watermark_resumes_after_the_newest_stored_row(tmp_path):
    """COL-02 acceptance c): rows up to T and no watermark: the first plan starts at T + step."""
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "resume.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        COLLECTION_SAFETY_LAG="1m",
        COLLECTION_MAX_BACKFILL="24h",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLUSTER_NAME="c",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    try:
        prom = FakePrometheus()
        prom.add(
            "container_cpu_usage_seconds_total", {"namespace": "ns", "pod": "p", "container": "app", "node": "n1"}, 0.1
        )
        metrics = SQLiteCombinedMetricsRepository(dbm)
        processor = build_range_processor(cfg, metrics, prom, FakeOpenCost())
        now = BASE + timedelta(hours=3, minutes=1)
        # Rows up to T = BASE + 2h written under a key that is gone (lost watermark).
        old = MetricsCollectionJob(
            processor=processor,
            metrics_repository=metrics,
            watermark_repository=create_collection_watermark_repository(dbm, "sqlite"),
            config=cfg,
            cluster_uid="lost",
            clock=lambda: BASE + timedelta(hours=2, minutes=6),
        )
        await old.run(last=timedelta(minutes=10))
        calls_before = len(processor.opencost_collector.calls)

        job = MetricsCollectionJob(
            processor=processor,
            metrics_repository=metrics,
            watermark_repository=create_collection_watermark_repository(dbm, "sqlite"),
            config=cfg,
            cluster_uid=UID_A,
            clock=lambda: now,
            window_repository=create_collection_window_repository(dbm, "sqlite"),
        )
        report = await job.run()

        assert report.succeeded, report.error
        assert report.planned_from == BASE + timedelta(hours=2, minutes=5)
        assert len(processor.opencost_collector.calls) - calls_before == 1
    finally:
        await dbm.close()
