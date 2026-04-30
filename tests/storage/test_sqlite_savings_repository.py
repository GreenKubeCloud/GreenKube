from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import aiosqlite
import pytest

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
                timestamp TEXT
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
                hour_bucket TEXT
            )
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
