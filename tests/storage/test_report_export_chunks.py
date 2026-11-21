"""The report export streams every stored row across its 7-day chunks (review of WP-05.3).

The export reads its range in 7-day windows; a closed window followed by a
cursor one second later dropped the sample on each boundary. With half-open
windows the export holds exactly the stored rows, and with ``include_idle``
the cluster total minus the idle share equals the sum of the exported rows.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.api.routers.report import report_export
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

START = (datetime.now(timezone.utc) - timedelta(days=20)).replace(hour=0, minute=0, second=0, microsecond=0)
DAYS = 10
STEPS = [START + timedelta(minutes=5 * i) for i in range(DAYS * 288)]


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL export tests.")
        schema = f"gk_export_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "export.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        config.reload()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


async def _seeded(manager: DatabaseManager):
    repo = (
        PostgresCombinedMetricsRepository(manager)
        if manager.db_type == "postgres"
        else SQLiteCombinedMetricsRepository(manager)
    )
    await repo.write_combined_metrics(
        [
            CombinedMetric(
                pod_name="api",
                namespace="prod",
                total_cost=0.01,
                co2e_grams=1.0,
                embodied_co2e_grams=0.1,
                joules=1500.0,
                cpu_request=500,
                timestamp=step,
                duration_seconds=300,
                node="node-a",
            )
            for step in STEPS
        ]
    )
    await repo.write_idle_metrics(
        [
            NodeIdleMetric(
                node="node-a",
                timestamp=step,
                duration_seconds=300,
                joules=4500.0,
                co2e_grams=3.0,
                embodied_co2e_grams=0.3,
                total_cost=0.03,
                cost_cpu=0.02,
                cost_ram=0.01,
                cost_source="modeled",
                cpu_unreserved_cores=1.5,
                memory_unreserved_bytes=2e9,
            )
            for step in STEPS
        ]
    )
    return repo


async def _export(repo, include_idle: bool) -> list[dict]:
    response = await report_export(
        namespace=None,
        last=None,
        start=START.isoformat(),
        end=(START + timedelta(days=DAYS)).isoformat(),
        years=None,
        aggregate=False,
        granularity=None,
        group_by="pod",
        fmt="json",
        include_idle=include_idle,
        repo=repo,
    )
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
    return json.loads(body)


async def test_export_keeps_every_stored_row_across_chunk_boundaries(db):
    repo = await _seeded(db)
    rows = await _export(repo, include_idle=False)
    assert len(rows) == len(STEPS)  # 2880, none lost on the 7-day boundary
    assert {row["row_kind"] for row in rows} == {"allocated"}
    assert sum(row["joules"] for row in rows) == pytest.approx(1500.0 * len(STEPS))


async def test_cluster_total_minus_idle_equals_the_exported_rows(db):
    repo = await _seeded(db)
    rows = await _export(repo, include_idle=True)
    allocated = [row for row in rows if row["row_kind"] == "allocated"]
    [idle] = [row for row in rows if row["row_kind"] == "idle"]
    [total] = [row for row in rows if row["row_kind"] == "cluster_total"]
    assert len(allocated) == len(STEPS)
    for column in ("joules", "co2e_grams", "embodied_co2e_grams", "total_cost"):
        exported = sum(row[column] for row in allocated)
        assert total[column] - idle[column] == pytest.approx(exported, rel=1e-9)
    assert idle["joules"] == pytest.approx(4500.0 * len(STEPS))
