"""The carbon-intensity recompute covers every raw table with a grid intensity (NRG-09).

Pod, container and ``__idle__`` rows of one step end with the same intensity and
source, so a node total (pods + idle) keeps conserving after consolidation. The
schema-driven test fails when a new raw table with ``grid_intensity`` is not
handled by the recompute.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCarbonIntensityRepository, PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCarbonIntensityRepository, SQLiteCombinedMetricsRepository

BASE = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
#: Raw tables the recompute refreshes; aggregates and intensity history are not raw rows.
RECOMPUTED = {"combined_metrics", "container_metrics", "node_idle_metrics"}
NOT_RAW = {"carbon_intensity_history"}


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL recompute tests.")
        schema = f"gk_idle_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "idle.sqlite"))
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


async def _fetch(manager: DatabaseManager, query: str) -> list[tuple]:
    async with manager.connection_scope() as connection:
        conn: Any = connection
        if manager.db_type == "postgres":
            return [tuple(row) for row in await conn.fetch(query)]
        async with conn.execute(query) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


async def _tables_with_grid_intensity(manager: DatabaseManager) -> set[str]:
    if manager.db_type == "postgres":
        rows = await _fetch(
            manager,
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'grid_intensity' AND table_schema = current_schema()",
        )
        return {row[0] for row in rows}
    tables = [row[0] for row in await _fetch(manager, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    found = set()
    for table in tables:
        columns = await _fetch(manager, f"PRAGMA table_info({table})")
        if any(column[1] == "grid_intensity" for column in columns):
            found.add(table)
    return found


async def test_every_raw_table_with_an_intensity_is_recomputed(db):
    tables = {t for t in await _tables_with_grid_intensity(db) if not t.endswith("_hourly")} - NOT_RAW
    assert tables == RECOMPUTED, "a raw table with grid_intensity is not handled by the carbon recompute"


async def test_pod_container_and_idle_rows_of_a_step_share_the_consolidated_intensity(db):
    if db.db_type == "postgres":
        carbon, repo = PostgresCarbonIntensityRepository(db), PostgresCombinedMetricsRepository(db)
    else:
        carbon, repo = SQLiteCarbonIntensityRepository(db), SQLiteCombinedMetricsRepository(db)
    step = BASE + timedelta(minutes=30)
    pod = CombinedMetric(
        pod_name="api",
        namespace="prod",
        total_cost=0.0,
        co2e_grams=3.6e6 / 3.6e6 * 1.2 * 99.0,
        pue=1.2,
        grid_intensity=99.0,
        joules=3.6e6,
        timestamp=step,
        emaps_zone="FR",
        duration_seconds=300,
    )
    await repo.write_combined_metrics([pod])
    await repo.write_container_metrics([pod.model_copy(update={"container_name": "app"})])
    idle = NodeIdleMetric(
        node="node-a",
        timestamp=step,
        duration_seconds=300,
        joules=7.2e6,
        co2e_grams=2 * 1.2 * 99.0,
        embodied_co2e_grams=0.0,
        total_cost=0.0,
        cost_cpu=0.0,
        cost_ram=0.0,
        cost_source=None,
        cpu_unreserved_cores=1.5,
        memory_unreserved_bytes=0.0,
        pue=1.2,
        grid_intensity=99.0,
        emaps_zone="FR",
        intensity_source="default",
    )
    await repo.write_idle_metrics([idle])
    await carbon.save_history(
        [
            {
                "carbonIntensity": 30.0,
                "datetime": BASE.isoformat(),
                "zone": "FR",
                "isEstimated": False,
                "estimationMethod": None,
                "emissionFactorType": "lifecycle",
            }
        ],
        "FR",
    )

    await repo.recompute_carbon_with_latest_intensities(carbon, BASE, BASE + timedelta(hours=1))

    rows = []
    for table in sorted(RECOMPUTED):
        rows += await _fetch(db, f"SELECT grid_intensity, intensity_source, co2e_grams, joules FROM {table}")
    assert {(row[0], row[1]) for row in rows} == {(30.0, "consolidated")}
    total_co2e = sum(row[2] for row in rows if row[3] == 7.2e6) + rows[0][2]
    assert total_co2e == pytest.approx((3.6e6 + 7.2e6) / 3.6e6 * 1.2 * 30.0, rel=1e-9)
