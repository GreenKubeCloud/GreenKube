"""PostgreSQL time buckets and years are UTC whatever the session time zone (STO-10)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository, _utc_trunc


def _row(pod: str, timestamp: datetime) -> CombinedMetric:
    return CombinedMetric(pod_name=pod, namespace="ns", timestamp=timestamp, joules=1.0, duration_seconds=300)


async def test_buckets_and_years_are_utc_for_a_role_in_another_time_zone(monkeypatch):
    dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run the PostgreSQL time zone test.")
    suffix = uuid4().hex[:10]
    role, schema, password = f"gk_tz_{suffix}", f"gk_tz_{suffix}", f"pw{suffix}"
    admin = await asyncpg.connect(dsn)
    parts = urlsplit(dsn)
    role_dsn = urlunsplit(parts._replace(netloc=f"{role}:{password}@{parts.hostname}:{parts.port}"))
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        await admin.execute(f"ALTER ROLE {role} SET timezone = 'Asia/Kolkata'")
        await admin.execute(f"CREATE SCHEMA {schema} AUTHORIZATION {role}")
        database = await admin.fetchval("SELECT current_database()")
        await admin.execute(f'GRANT CREATE ON DATABASE "{database}" TO {role}')
        monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", role_dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
        monkeypatch.setenv("METRICS_COMPRESSION_AGE_HOURS", "100000")
        config.reload()
        manager = DatabaseManager(Config())
        await manager.connect()
        try:
            repo = PostgresCombinedMetricsRepository(manager)
            await repo.write_combined_metrics(
                [
                    _row("a", datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)),
                    _row("b", datetime(2026, 11, 15, 0, 30, tzinfo=timezone.utc)),
                    _row("c", datetime(2025, 12, 31, 20, 0, tzinfo=timezone.utc)),
                ]
            )
            start = datetime(2026, 11, 14, tzinfo=timezone.utc)
            end = datetime(2026, 11, 16, tzinfo=timezone.utc)
            daily = await repo.aggregate_timeseries(start, end, granularity="day")
            assert [(b["timestamp"], b["energy_joules"]) for b in daily] == [
                ("2026-11-14T00:00:00Z", 1.0),
                ("2026-11-15T00:00:00Z", 1.0),
            ]
            hourly = await repo.aggregate_timeseries(start, end, granularity="hour")
            assert [b["timestamp"] for b in hourly] == ["2026-11-14T23:00:00Z", "2026-11-15T00:00:00Z"]
            assert await repo.list_metric_years() == [2026, 2025]
        finally:
            await manager.close()
        # The bucket SQL itself does not depend on the session time zone either.
        await admin.execute("SET timezone = 'Asia/Kolkata'")
        late = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)
        bucket = await admin.fetchval(f"SELECT {_utc_trunc('day', '$1::timestamptz')}", late)
        assert bucket == datetime(2026, 11, 14, tzinfo=timezone.utc)
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.execute(f"DROP OWNED BY {role} CASCADE")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
