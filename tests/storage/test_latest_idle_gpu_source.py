"""The GPU power source of each node's latest idle row, for ``GET /nodes`` (NRG-13).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.impact.rows import NodeIdleMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def repo(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_idle_gpu_src_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "idle_gpu_source.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        if manager.db_type == "postgres":
            yield PostgresCombinedMetricsRepository(manager)
        else:
            yield SQLiteCombinedMetricsRepository(manager)
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _idle(node: str, minutes_ago: int, gpu_source: Optional[str]) -> NodeIdleMetric:
    return NodeIdleMetric(
        node=node,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        duration_seconds=300,
        joules=0.0,
        co2e_grams=0.0,
        embodied_co2e_grams=0.0,
        total_cost=0.0,
        cost_cpu=0.0,
        cost_ram=0.0,
        cost_source="none",
        cpu_unreserved_cores=0.0,
        memory_unreserved_bytes=0,
        gpu_source=gpu_source,
    )


async def test_latest_gpu_source_per_node_in_the_window(repo):
    await repo.write_idle_metrics(
        [
            _idle("gpu-1", 10, "dcgm"),
            _idle("gpu-1", 5, "gpu_tdp_estimate"),
            _idle("gpu-2", 5, "unmeasured"),
            _idle("gpu-2", 0, None),
            _idle("old", 120, "unmeasured"),
        ]
    )
    sources = await repo.read_latest_idle_gpu_sources(NOW - timedelta(hours=1), NOW + timedelta(seconds=1))
    assert sources == {"gpu-1": "gpu_tdp_estimate", "gpu-2": None}
