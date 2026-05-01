"""Real database contract tests for SQLite and PostgreSQL storage adapters."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    CombinedMetric,
    IgnoreRecommendationRequest,
    MetricsSummaryRow,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
    TimeseriesCachePoint,
)
from greenkube.models.node import NodeInfo
from greenkube.models.savings import SavingsLedgerRecord
from greenkube.storage.embodied_repository import PostgresEmbodiedRepository, SQLiteEmbodiedRepository
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.postgres.recommendation_repository import PostgresRecommendationRepository
from greenkube.storage.postgres.repository import PostgresCarbonIntensityRepository, PostgresCombinedMetricsRepository
from greenkube.storage.postgres.savings_repository import PostgresSavingsLedgerRepository
from greenkube.storage.postgres.summary_repository import PostgresSummaryRepository
from greenkube.storage.postgres.timeseries_cache_repository import PostgresTimeseriesCacheRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.repository import SQLiteCarbonIntensityRepository, SQLiteCombinedMetricsRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.storage.sqlite.summary_repository import SQLiteSummaryRepository
from greenkube.storage.sqlite.timeseries_cache_repository import SQLiteTimeseriesCacheRepository


@dataclass
class RealDatabase:
    """A connected real database backend and its repository implementations."""

    backend: str
    manager: DatabaseManager
    schema: str | None = None
    postgres_dsn: str | None = None

    def carbon_repository(self):
        if self.backend == "postgres":
            return PostgresCarbonIntensityRepository(self.manager)
        return SQLiteCarbonIntensityRepository(self.manager)

    def combined_repository(self):
        if self.backend == "postgres":
            return PostgresCombinedMetricsRepository(self.manager)
        return SQLiteCombinedMetricsRepository(self.manager)

    def node_repository(self):
        if self.backend == "postgres":
            return PostgresNodeRepository(self.manager)
        return SQLiteNodeRepository(self.manager)

    def recommendation_repository(self):
        if self.backend == "postgres":
            return PostgresRecommendationRepository(self.manager)
        return SQLiteRecommendationRepository(self.manager)

    def summary_repository(self):
        if self.backend == "postgres":
            return PostgresSummaryRepository(self.manager)
        return SQLiteSummaryRepository(self.manager)

    def timeseries_cache_repository(self):
        if self.backend == "postgres":
            return PostgresTimeseriesCacheRepository(self.manager)
        return SQLiteTimeseriesCacheRepository(self.manager)

    def savings_repository(self):
        if self.backend == "postgres":
            return PostgresSavingsLedgerRepository(self.manager)
        return SQLiteSavingsLedgerRepository(self.manager)

    def embodied_repository(self):
        if self.backend == "postgres":
            return PostgresEmbodiedRepository(self.manager)
        return SQLiteEmbodiedRepository(self.manager)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def real_database(request, tmp_path, monkeypatch) -> RealDatabase:
    """Create an isolated real database for each storage contract test."""
    backend = request.param
    postgres_dsn = None
    schema = None

    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "30000")
    monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "24")

    if backend == "postgres":
        postgres_dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not postgres_dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run real PostgreSQL integration tests.")
        schema = f"gk_test_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", postgres_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
    else:
        db_path = tmp_path / "greenkube.sqlite"
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(db_path))
        monkeypatch.setenv("DB_SCHEMA", "public")

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()

    database = RealDatabase(backend=backend, manager=manager, schema=schema, postgres_dsn=postgres_dsn)
    try:
        yield database
    finally:
        await manager.close()
        if backend == "postgres" and postgres_dsn and schema:
            conn = await asyncpg.connect(postgres_dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def _table_names(database: RealDatabase) -> set[str]:
    async with database.manager.connection_scope() as conn:
        if database.backend == "postgres":
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
                database.schema,
            )
            return {row["table_name"] for row in rows}

        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in await cursor.fetchall()}


async def _migration_versions(database: RealDatabase) -> set[int]:
    async with database.manager.connection_scope() as conn:
        if database.backend == "postgres":
            rows = await conn.fetch("SELECT version FROM schema_migrations")
            return {row["version"] for row in rows}

        cursor = await conn.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in await cursor.fetchall()}


def _metric(
    pod_name: str,
    namespace: str,
    timestamp: datetime,
    co2e_grams: float,
    total_cost: float,
    joules: float,
) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod_name,
        namespace=namespace,
        total_cost=total_cost,
        co2e_grams=co2e_grams,
        pue=1.2,
        grid_intensity=50.0,
        joules=joules,
        cpu_request=250,
        memory_request=512 * 1024 * 1024,
        cpu_usage_millicores=120,
        memory_usage_bytes=256 * 1024 * 1024,
        network_receive_bytes=10.0,
        network_transmit_bytes=20.0,
        disk_read_bytes=30.0,
        disk_write_bytes=40.0,
        restart_count=1,
        owner_kind="Deployment",
        owner_name=f"{pod_name}-deployment",
        timestamp=timestamp,
        duration_seconds=300,
        grid_intensity_timestamp=timestamp,
        node="node-a",
        node_instance_type="m5.large",
        node_zone="eu-west-3a",
        emaps_zone="FR",
        is_estimated=True,
        estimation_reasons=["test-estimate"],
        embodied_co2e_grams=2.5,
        calculation_version="contract-test",
    )


def _recommendation_record(
    pod_name: str = "api-pod",
    namespace: str = "prod",
    rec_type: RecommendationType = RecommendationType.RIGHTSIZING_CPU,
    created_at: datetime | None = None,
) -> RecommendationRecord:
    return RecommendationRecord(
        pod_name=pod_name,
        namespace=namespace,
        type=rec_type,
        description=f"Optimize {pod_name}",
        reason="Contract test recommendation",
        priority="high",
        potential_savings_cost=3.5,
        potential_savings_co2e_grams=42.0,
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=250,
        created_at=created_at or datetime.now(timezone.utc),
    )


async def _insert_hourly_metric(
    database: RealDatabase,
    pod_name: str,
    namespace: str,
    hour_bucket: datetime,
    co2e_grams: float,
    total_cost: float,
    joules: float,
    estimation_reasons: str = '["hourly-rollup"]',
) -> None:
    async with database.manager.connection_scope() as conn:
        if database.backend == "postgres":
            await conn.execute(
                """
                INSERT INTO combined_metrics_hourly (
                    pod_name, namespace, hour_bucket, sample_count,
                    total_cost, co2e_grams, embodied_co2e_grams,
                    pue, grid_intensity, joules,
                    cpu_request, memory_request,
                    cpu_usage_avg, cpu_usage_max,
                    memory_usage_avg, memory_usage_max,
                    network_receive_bytes, network_transmit_bytes,
                    disk_read_bytes, disk_write_bytes,
                    storage_request_bytes, storage_usage_bytes,
                    gpu_usage_millicores, restart_count,
                    owner_kind, owner_name,
                    duration_seconds, node, node_instance_type,
                    node_zone, emaps_zone, is_estimated,
                    estimation_reasons, calculation_version
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7,
                    $8, $9, $10,
                    $11, $12,
                    $13, $14,
                    $15, $16,
                    $17, $18,
                    $19, $20,
                    $21, $22,
                    $23, $24,
                    $25, $26,
                    $27, $28, $29,
                    $30, $31, $32,
                    $33, $34
                )
                """,
                pod_name,
                namespace,
                hour_bucket,
                3,
                total_cost,
                co2e_grams,
                1.5,
                1.2,
                50.0,
                joules,
                250,
                512 * 1024 * 1024,
                125,
                200,
                256 * 1024 * 1024,
                300 * 1024 * 1024,
                10.0,
                20.0,
                30.0,
                40.0,
                1024,
                512,
                0,
                1,
                "Deployment",
                f"{pod_name}-deployment",
                900,
                "node-a",
                "m5.large",
                "eu-west-3a",
                "FR",
                True,
                estimation_reasons,
                "hourly-contract-test",
            )
            return

        await conn.execute(
            """
            INSERT INTO combined_metrics_hourly (
                pod_name, namespace, hour_bucket, sample_count,
                total_cost, co2e_grams, embodied_co2e_grams,
                pue, grid_intensity, joules,
                cpu_request, memory_request,
                cpu_usage_avg, cpu_usage_max,
                memory_usage_avg, memory_usage_max,
                network_receive_bytes, network_transmit_bytes,
                disk_read_bytes, disk_write_bytes,
                storage_request_bytes, storage_usage_bytes,
                gpu_usage_millicores, restart_count,
                owner_kind, owner_name,
                duration_seconds, node, node_instance_type,
                node_zone, emaps_zone, is_estimated,
                estimation_reasons, calculation_version
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                pod_name,
                namespace,
                hour_bucket.isoformat(),
                3,
                total_cost,
                co2e_grams,
                1.5,
                1.2,
                50.0,
                joules,
                250,
                512 * 1024 * 1024,
                125,
                200,
                256 * 1024 * 1024,
                300 * 1024 * 1024,
                10.0,
                20.0,
                30.0,
                40.0,
                1024,
                512,
                0,
                1,
                "Deployment",
                f"{pod_name}-deployment",
                900,
                "node-a",
                "m5.large",
                "eu-west-3a",
                "FR",
                1,
                estimation_reasons,
                "hourly-contract-test",
            ),
        )
        await conn.commit()


async def _insert_namespace_cache(database: RealDatabase, namespaces: list[str]) -> None:
    async with database.manager.connection_scope() as conn:
        if database.backend == "postgres":
            for namespace in namespaces:
                await conn.execute(
                    """
                    INSERT INTO namespace_cache (namespace, last_seen)
                    VALUES ($1, $2)
                    ON CONFLICT (namespace) DO UPDATE SET last_seen = EXCLUDED.last_seen
                    """,
                    namespace,
                    datetime.now(timezone.utc),
                )
            return

        for namespace in namespaces:
            await conn.execute(
                """
                INSERT INTO namespace_cache (namespace, last_seen)
                VALUES (?, ?)
                ON CONFLICT(namespace) DO UPDATE SET last_seen = excluded.last_seen
                """,
                (namespace, datetime.now(timezone.utc).isoformat()),
            )
        await conn.commit()


@pytest.mark.asyncio
@pytest.mark.database
async def test_database_manager_creates_schema_and_records_migrations(real_database: RealDatabase):
    tables = await _table_names(real_database)
    assert {
        "carbon_intensity_history",
        "combined_metrics",
        "combined_metrics_hourly",
        "metrics_summary",
        "metrics_timeseries_cache",
        "namespace_cache",
        "node_snapshots",
        "node_snapshots_scd",
        "recommendation_history",
        "recommendation_savings_ledger",
        "schema_migrations",
    }.issubset(tables)
    assert set(range(1, 9)).issubset(await _migration_versions(real_database))


@pytest.mark.asyncio
@pytest.mark.database
async def test_carbon_intensity_repository_round_trips_and_upserts(real_database: RealDatabase):
    repo = real_database.carbon_repository()
    first = "2026-04-30T08:00:00Z"
    second = "2026-04-30T09:00:00Z"

    saved = await repo.save_history(
        [
            {"datetime": first, "carbonIntensity": 51.5, "updatedAt": first, "isEstimated": False},
            {"datetime": second, "carbonIntensity": 62.0, "updatedAt": second, "isEstimated": True},
        ],
        zone="FR",
    )
    assert saved == 2
    assert await repo.get_for_zone_at_time("FR", "2026-04-30T08:30:00Z") == pytest.approx(51.5)

    await repo.save_history(
        [{"datetime": first, "carbonIntensity": 55.0, "updatedAt": second, "isEstimated": True}],
        zone="FR",
    )
    assert await repo.get_for_zone_at_time("FR", "2026-04-30T08:30:00Z") == pytest.approx(55.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_combined_metrics_repository_round_trips_and_aggregates(real_database: RealDatabase):
    repo = real_database.combined_repository()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=30)
    metrics = [
        _metric("api-pod", "prod", now - timedelta(minutes=20), 20.0, 1.5, 1000.0),
        _metric("worker-pod", "dev", now - timedelta(minutes=10), 10.0, 0.5, 500.0),
    ]

    assert await repo.write_combined_metrics(metrics) == 2

    rows = await repo.read_combined_metrics(start, now)
    assert {row.pod_name for row in rows} == {"api-pod", "worker-pod"}
    assert rows[0].calculation_version == "contract-test"

    summary = await repo.aggregate_summary(start, now)
    assert summary["total_co2e_grams"] == pytest.approx(30.0)
    assert summary["total_embodied_co2e_grams"] == pytest.approx(5.0)
    assert summary["namespace_count"] == 2

    prod_summary = await repo.aggregate_summary(start, now, namespace="prod")
    assert prod_summary["total_cost"] == pytest.approx(1.5)
    assert prod_summary["pod_count"] == 1

    namespaces = await repo.list_namespaces()
    assert namespaces == ["dev", "prod"]

    top_pods = await repo.aggregate_top_pods(start, now, limit=1)
    assert top_pods[0]["pod_name"] == "api-pod"
    assert top_pods[0]["co2e_grams"] == pytest.approx(20.0)

    timeseries = await repo.aggregate_timeseries(start, now, granularity="hour")
    assert sum(point["co2e_grams"] for point in timeseries) == pytest.approx(30.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_combined_metrics_repository_reads_hourly_rollups(real_database: RealDatabase):
    repo = real_database.combined_repository()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old_hour = (now - timedelta(hours=30)).replace(minute=0, second=0, microsecond=0)
    old_window_start = old_hour - timedelta(minutes=5)
    old_window_end = old_hour + timedelta(minutes=55)

    await _insert_hourly_metric(real_database, "api-hourly", "prod", old_hour, 12.0, 1.2, 1200.0)
    await _insert_hourly_metric(real_database, "worker-hourly", "dev", old_hour, 8.0, 0.8, 800.0, "not-json")

    prod_hourly = await repo.read_hourly_metrics(old_window_start, old_window_end, namespace="prod")
    assert len(prod_hourly) == 1
    assert prod_hourly[0].pod_name == "api-hourly"
    assert prod_hourly[0].estimation_reasons == ["hourly-rollup"]
    assert prod_hourly[0].calculation_version == "hourly-contract-test"

    all_hourly = await repo.read_hourly_metrics(old_window_start, old_window_end)
    invalid_json_row = next(metric for metric in all_hourly if metric.pod_name == "worker-hourly")
    assert invalid_json_row.estimation_reasons == []

    summary = await repo.aggregate_summary(old_window_start, old_window_end, namespace="prod")
    assert summary["total_co2e_grams"] == pytest.approx(12.0)
    assert summary["total_embodied_co2e_grams"] == pytest.approx(1.5)
    assert summary["pod_count"] == 1

    timeseries = await repo.aggregate_timeseries(old_window_start, old_window_end, granularity="week", namespace="prod")
    assert len(timeseries) == 1
    assert timeseries[0]["co2e_grams"] == pytest.approx(12.0)
    assert timeseries[0]["energy_joules"] == pytest.approx(1200.0)

    by_namespace = await repo.aggregate_by_namespace(old_window_start, old_window_end, namespace="prod")
    assert by_namespace == [
        {
            "namespace": "prod",
            "co2e_grams": pytest.approx(12.0),
            "embodied_co2e_grams": pytest.approx(1.5),
            "total_cost": pytest.approx(1.2),
            "energy_joules": pytest.approx(1200.0),
        }
    ]

    top_pods = await repo.aggregate_top_pods(old_window_start, old_window_end, namespace="prod", limit=5)
    assert len(top_pods) == 1
    assert top_pods[0]["pod_name"] == "api-hourly"
    assert top_pods[0]["co2e_grams"] == pytest.approx(12.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_combined_metrics_repository_namespace_cache_and_empty_ranges(real_database: RealDatabase):
    repo = real_database.combined_repository()
    await _insert_namespace_cache(real_database, ["cache-a", "cache-b"])

    assert await repo.list_namespaces() == ["cache-a", "cache-b"]

    now = datetime.now(timezone.utc).replace(microsecond=0)
    inverted_start = now
    inverted_end = now - timedelta(hours=30)

    empty_summary = await repo.aggregate_summary(inverted_start, inverted_end)
    assert empty_summary == {
        "total_co2e_grams": 0.0,
        "total_embodied_co2e_grams": 0.0,
        "total_cost": 0.0,
        "total_energy_joules": 0.0,
        "pod_count": 0,
        "namespace_count": 0,
    }
    assert await repo.aggregate_timeseries(inverted_start, inverted_end) == []
    assert await repo.aggregate_by_namespace(inverted_start, inverted_end) == []
    assert await repo.aggregate_top_pods(inverted_start, inverted_end) == []


@pytest.mark.asyncio
@pytest.mark.database
async def test_node_repository_tracks_scd2_changes(real_database: RealDatabase):
    repo = real_database.node_repository()
    first_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=20)
    second_ts = first_ts + timedelta(minutes=10)
    node = NodeInfo(
        name="node-a",
        instance_type="m5.large",
        zone="eu-west-3a",
        region="eu-west-3",
        cloud_provider="aws",
        architecture="amd64",
        node_pool="default",
        cpu_capacity_cores=2.0,
        memory_capacity_bytes=8 * 1024 * 1024 * 1024,
        timestamp=first_ts,
        embodied_emissions_kg=100.0,
    )

    assert await repo.save_nodes([node]) == 1
    assert await repo.save_nodes([node]) == 0

    changed = node.model_copy(update={"instance_type": "m5.xlarge", "cpu_capacity_cores": 4.0, "timestamp": second_ts})
    assert await repo.save_nodes([changed]) == 1

    latest = await repo.get_latest_snapshots_before(second_ts + timedelta(minutes=1))
    assert len(latest) == 1
    assert latest[0].instance_type == "m5.xlarge"

    snapshots = await repo.get_snapshots(first_ts - timedelta(minutes=1), second_ts + timedelta(minutes=1))
    assert len(snapshots) == 2


@pytest.mark.asyncio
@pytest.mark.database
async def test_recommendation_repository_lifecycle_round_trip(real_database: RealDatabase):
    repo = real_database.recommendation_repository()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    record = _recommendation_record(created_at=now - timedelta(minutes=10))
    assert await repo.upsert_recommendations([record]) == 1
    assert await repo.upsert_recommendations([record.model_copy(update={"description": "Updated recommendation"})]) == 1

    active = await repo.get_active_recommendations(namespace="prod")
    assert len(active) == 1
    assert active[0].description == "Updated recommendation"

    applied = await repo.apply_recommendation(
        active[0].id,
        ApplyRecommendationRequest(actual_cpu_request_millicores=300, carbon_saved_co2e_grams=30.0, cost_saved=2.0),
    )
    assert applied.status == RecommendationStatus.APPLIED

    savings = await repo.get_savings_summary(
        namespace="prod", start=now - timedelta(hours=1), end=now + timedelta(hours=1)
    )
    assert savings.applied_count == 1
    assert savings.total_carbon_saved_co2e_grams == pytest.approx(30.0)
    assert savings.total_cost_saved == pytest.approx(2.0)

    ignored_seed = _recommendation_record(
        pod_name="worker-pod",
        rec_type=RecommendationType.ZOMBIE_POD,
        created_at=now,
    )
    await repo.save_recommendations([ignored_seed])
    worker = (await repo.get_recommendations(now - timedelta(minutes=1), now + timedelta(minutes=1), namespace="prod"))[
        0
    ]
    ignored = await repo.ignore_recommendation(worker.id, IgnoreRecommendationRequest(reason="intentional"))
    assert ignored.status == RecommendationStatus.IGNORED
    assert len(await repo.get_ignored_recommendations(namespace="prod")) == 1

    restored = await repo.unignore_recommendation(worker.id)
    assert restored.status == RecommendationStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.database
async def test_summary_and_timeseries_cache_repositories_upsert(real_database: RealDatabase):
    summary_repo = real_database.summary_repository()
    timeseries_repo = real_database.timeseries_cache_repository()
    updated_at = datetime.now(timezone.utc).replace(microsecond=0)

    await summary_repo.upsert_row(
        MetricsSummaryRow(
            window_slug="24h",
            namespace=None,
            total_co2e_grams=10.0,
            total_embodied_co2e_grams=2.0,
            total_cost=1.0,
            total_energy_joules=100.0,
            pod_count=1,
            namespace_count=1,
            updated_at=updated_at,
        )
    )
    await summary_repo.upsert_row(
        MetricsSummaryRow(
            window_slug="24h",
            namespace=None,
            total_co2e_grams=15.0,
            total_embodied_co2e_grams=3.0,
            total_cost=2.0,
            total_energy_joules=150.0,
            pod_count=2,
            namespace_count=1,
            updated_at=updated_at + timedelta(minutes=1),
        )
    )

    cluster_rows = await summary_repo.get_rows(namespace=None)
    assert len(cluster_rows) == 1
    assert cluster_rows[0].total_co2e_grams == pytest.approx(15.0)
    assert cluster_rows[0].total_co2e_all_scopes == pytest.approx(18.0)

    await timeseries_repo.upsert_points(
        [
            TimeseriesCachePoint(
                window_slug="24h",
                namespace=None,
                bucket_ts="2026-04-30T08:00:00Z",
                co2e_grams=1.0,
                embodied_co2e_grams=0.2,
                total_cost=0.1,
                joules=10.0,
            ),
            TimeseriesCachePoint(
                window_slug="24h",
                namespace=None,
                bucket_ts="2026-04-30T09:00:00Z",
                co2e_grams=2.0,
                embodied_co2e_grams=0.4,
                total_cost=0.2,
                joules=20.0,
            ),
        ]
    )
    await timeseries_repo.upsert_points(
        [
            TimeseriesCachePoint(
                window_slug="24h",
                namespace=None,
                bucket_ts="2026-04-30T10:00:00Z",
                co2e_grams=3.0,
                embodied_co2e_grams=0.6,
                total_cost=0.3,
                joules=30.0,
            )
        ]
    )

    points = await timeseries_repo.get_points("24h", namespace=None)
    assert len(points) == 1
    assert points[0].bucket_ts == "2026-04-30T10:00:00Z"
    assert points[0].co2e_grams == pytest.approx(3.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_savings_ledger_repository_keeps_totals_across_compression(real_database: RealDatabase):
    repo = real_database.savings_repository()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    records = [
        SavingsLedgerRecord(
            recommendation_id=1,
            cluster_name="contract-cluster",
            namespace="prod",
            recommendation_type="RIGHTSIZING_CPU",
            co2e_saved_grams=4.0,
            cost_saved_dollars=1.0,
            timestamp=now - timedelta(hours=30),
        ),
        SavingsLedgerRecord(
            recommendation_id=2,
            cluster_name="contract-cluster",
            namespace="prod",
            recommendation_type="RIGHTSIZING_CPU",
            co2e_saved_grams=6.0,
            cost_saved_dollars=2.0,
            timestamp=now - timedelta(hours=1),
        ),
    ]

    assert await repo.save_records(records) == 2
    totals = await repo.get_cumulative_totals("contract-cluster")
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(10.0)

    assert await repo.compress_to_hourly(cutoff_hours=24) >= 1
    compressed_totals = await repo.get_cumulative_totals("contract-cluster")
    assert compressed_totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(10.0)

    window_totals = await repo.get_window_totals(
        "contract-cluster",
        now - timedelta(hours=48),
        now,
        namespace="prod",
    )
    assert window_totals["RIGHTSIZING_CPU"]["cost_saved_dollars"] == pytest.approx(3.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_embodied_repository_round_trips_and_upserts(real_database: RealDatabase):
    repo = real_database.embodied_repository()

    assert await repo.get_profile("aws", "missing.instance") is None

    await repo.save_profile("aws", "m5.large", gwp=950.0, lifespan=35_040, source="contract-test")
    first = await repo.get_profile("aws", "m5.large")
    assert first is not None
    assert first["gwp_manufacture"] == pytest.approx(950.0)
    assert first["lifespan_hours"] == 35_040
    assert first["source"] == "contract-test"
    assert first["last_updated"] is not None

    await repo.save_profile("aws", "m5.large", gwp=1000.0, lifespan=40_000, source="contract-update")
    updated = await repo.get_profile("aws", "m5.large")
    assert updated is not None
    assert updated["gwp_manufacture"] == pytest.approx(1000.0)
    assert updated["lifespan_hours"] == 40_000
    assert updated["source"] == "contract-update"
