from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import aiosqlite
import pytest

from greenkube.models.savings import SavingsLedgerRecord
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.utils.date_utils import to_iso_z


@pytest.fixture
async def db_connection():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute(
            """
            CREATE TABLE recommendation_savings_ledger (
                recommendation_id TEXT,
                cluster_name TEXT,
                namespace TEXT,
                recommendation_type TEXT,
                co2e_saved_grams REAL,
                cost_saved_dollars REAL,
                period_seconds INTEGER,
                timestamp TEXT,
                period_start TEXT,
                period_end TEXT,
                measurement_method TEXT DEFAULT 'prorated',
                baseline_value REAL,
                actual_value REAL,
                confidence REAL,
                superseded INTEGER DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE recommendation_savings_ledger_hourly (
                recommendation_id TEXT,
                cluster_name TEXT,
                namespace TEXT,
                recommendation_type TEXT,
                co2e_saved_grams REAL,
                cost_saved_dollars REAL,
                sample_count INTEGER,
                hour_bucket TEXT,
                measurement_method TEXT DEFAULT 'prorated',
                baseline_value REAL,
                actual_value REAL,
                confidence REAL,
                superseded INTEGER DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX uq_savings_ledger_period
            ON recommendation_savings_ledger
            (recommendation_id, period_start, period_end, measurement_method)
            """
        )
        await conn.execute(
            """
            ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN period_start TEXT
            """
        )
        await conn.execute(
            """
            ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN period_end TEXT
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX uq_savings_ledger_hourly_period
            ON recommendation_savings_ledger_hourly
            (recommendation_id, hour_bucket, measurement_method)
            """
        )
        await conn.commit()
        yield conn


@pytest.fixture
async def sqlite_savings_repo(db_connection):
    db_manager = MagicMock()

    @asynccontextmanager
    async def scope():
        yield db_connection

    db_manager.connection_scope = scope
    return SQLiteSavingsLedgerRepository(db_manager)


@pytest.mark.asyncio
async def test_get_window_totals_filters_namespace_across_raw_and_hourly(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    sample_time = start + timedelta(minutes=15)
    outside_time = start - timedelta(minutes=1)

    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, period_seconds, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("raw-prod", "minikube", "prod", "RIGHTSIZING_CPU", 10.0, 1.0, 300, to_iso_z(sample_time)),
            ("raw-dev", "minikube", "dev", "RIGHTSIZING_CPU", 20.0, 2.0, 300, to_iso_z(sample_time)),
            ("raw-old", "minikube", "prod", "RIGHTSIZING_CPU", 99.0, 9.9, 300, to_iso_z(outside_time)),
        ],
    )
    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger_hourly
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, sample_count, hour_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("hourly-prod", "minikube", "prod", "RIGHTSIZING_CPU", 5.0, 0.5, 1, to_iso_z(sample_time)),
            ("hourly-dev", "minikube", "dev", "RIGHTSIZING_MEMORY", 7.0, 0.7, 1, to_iso_z(sample_time)),
        ],
    )
    await db_connection.commit()

    totals = await sqlite_savings_repo.get_window_totals(
        cluster_name="minikube",
        start_time=start,
        end_time=end,
        namespace="prod",
    )

    assert totals == {"RIGHTSIZING_CPU": {"co2e_saved_grams": 15.0, "cost_saved_dollars": 1.5}}


@pytest.mark.asyncio
async def test_save_records_is_idempotent_for_explicit_period(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    record = SavingsLedgerRecord(
        recommendation_id=1,
        cluster_name="minikube",
        namespace="prod",
        recommendation_type="RIGHTSIZING_CPU",
        co2e_saved_grams=10.0,
        period_seconds=300,
        timestamp=end,
        period_start=start,
        period_end=end,
    )

    assert await sqlite_savings_repo.save_records([record]) == 1
    record.co2e_saved_grams = 12.0
    assert await sqlite_savings_repo.save_records([record]) == 1
    cursor = await db_connection.execute("SELECT COUNT(*), MAX(co2e_saved_grams) FROM recommendation_savings_ledger")
    assert await cursor.fetchone() == (1, 12.0)


@pytest.mark.asyncio
async def test_measured_record_supersedes_overlapping_prorated_record(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    prorated = SavingsLedgerRecord(
        recommendation_id=1,
        cluster_name="minikube",
        recommendation_type="RIGHTSIZING_CPU",
        co2e_saved_grams=10.0,
        timestamp=end,
        period_start=start,
        period_end=end,
    )
    measured = prorated.model_copy(update={"measurement_method": "measured", "co2e_saved_grams": 7.0})

    await sqlite_savings_repo.save_records([prorated, measured])
    totals = await sqlite_savings_repo.get_cumulative_totals("minikube")
    assert totals["RIGHTSIZING_CPU"]["co2e_saved_grams"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_get_window_totals_keeps_all_namespaces_when_unfiltered(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    sample_time = start + timedelta(minutes=15)

    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, period_seconds, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("raw-prod", "minikube", "prod", "RIGHTSIZING_CPU", 10.0, 1.0, 300, to_iso_z(sample_time)),
            ("raw-dev", "minikube", "dev", "RIGHTSIZING_CPU", 20.0, 2.0, 300, to_iso_z(sample_time)),
        ],
    )
    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger_hourly
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, sample_count, hour_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [("hourly-dev", "minikube", "dev", "RIGHTSIZING_MEMORY", 7.0, 0.7, 1, to_iso_z(sample_time))],
    )
    await db_connection.commit()

    totals = await sqlite_savings_repo.get_window_totals(cluster_name="minikube", start_time=start, end_time=end)

    assert totals == {
        "RIGHTSIZING_CPU": {"co2e_saved_grams": 30.0, "cost_saved_dollars": 3.0},
        "RIGHTSIZING_MEMORY": {"co2e_saved_grams": 7.0, "cost_saved_dollars": 0.7},
    }


@pytest.mark.asyncio
async def test_get_window_totals_filters_cluster_scoped_rows(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    sample_time = start + timedelta(minutes=15)

    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, period_seconds, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("raw-empty", "minikube", "", "OVERPROVISIONED_NODE", 10.0, 1.0, 300, to_iso_z(sample_time)),
            ("raw-null", "minikube", None, "OVERPROVISIONED_NODE", 5.0, 0.5, 300, to_iso_z(sample_time)),
            ("raw-prod", "minikube", "prod", "OVERPROVISIONED_NODE", 20.0, 2.0, 300, to_iso_z(sample_time)),
        ],
    )
    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger_hourly
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, sample_count, hour_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [("hourly-empty", "minikube", "", "OVERPROVISIONED_NODE", 2.0, 0.2, 1, to_iso_z(sample_time))],
    )
    await db_connection.commit()

    totals = await sqlite_savings_repo.get_window_totals(
        cluster_name="minikube",
        start_time=start,
        end_time=end,
        namespace="",
    )

    assert totals == {"OVERPROVISIONED_NODE": {"co2e_saved_grams": 17.0, "cost_saved_dollars": 1.7}}


@pytest.mark.asyncio
async def test_supersede_excludes_rows_from_totals(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    sample_time = start + timedelta(minutes=15)

    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, period_seconds, timestamp, measurement_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "minikube", "prod", "RIGHTSIZING_CPU", 10.0, 1.0, 300, to_iso_z(sample_time), "prorated"),
            (2, "minikube", "prod", "RIGHTSIZING_CPU", 20.0, 2.0, 300, to_iso_z(sample_time), "measured"),
        ],
    )
    await db_connection.commit()

    flagged = await sqlite_savings_repo.supersede_for_recommendation(1)
    assert flagged == 1

    totals = await sqlite_savings_repo.get_window_totals(cluster_name="minikube", start_time=start, end_time=end)
    assert totals == {"RIGHTSIZING_CPU": {"co2e_saved_grams": 20.0, "cost_saved_dollars": 2.0}}

    by_method = await sqlite_savings_repo.get_window_totals(
        cluster_name="minikube", start_time=start, end_time=end, group_by_method=True
    )
    assert by_method == {"measured": {"co2e_saved_grams": 20.0, "cost_saved_dollars": 2.0}}


@pytest.mark.asyncio
async def test_get_window_totals_group_by_method(sqlite_savings_repo, db_connection):
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    sample_time = start + timedelta(minutes=15)

    await db_connection.executemany(
        """
        INSERT INTO recommendation_savings_ledger
            (recommendation_id, cluster_name, namespace, recommendation_type,
             co2e_saved_grams, cost_saved_dollars, period_seconds, timestamp, measurement_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("r1", "minikube", "prod", "RIGHTSIZING_CPU", 10.0, 1.0, 300, to_iso_z(sample_time), "prorated"),
            ("r2", "minikube", "prod", "RIGHTSIZING_CPU", 4.0, 0.4, 300, to_iso_z(sample_time), "measured"),
        ],
    )
    await db_connection.commit()

    by_method = await sqlite_savings_repo.get_window_totals(
        cluster_name="minikube", start_time=start, end_time=end, group_by_method=True
    )
    assert by_method == {
        "prorated": {"co2e_saved_grams": 10.0, "cost_saved_dollars": 1.0},
        "measured": {"co2e_saved_grams": 4.0, "cost_saved_dollars": 0.4},
    }
