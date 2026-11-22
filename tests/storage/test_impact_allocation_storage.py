"""Storage contract for Impact Model allocation (roadmap B2, B5, B8) on SQLite and PostgreSQL.

* raw pod and container rows persist ``model_version`` and ``gpu_power_watts``;
* ``node_idle_metrics`` stores one ``__idle__`` row per node and step, upserted.
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
from greenkube.impact import IMPACT_MODEL_VERSION
from greenkube.impact.rows import NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

T0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def repo(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_impact_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "impact.sqlite"))
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


def _row(timestamp: datetime, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {
        "pod_name": "train-0",
        "namespace": "ml",
        "timestamp": timestamp,
        "duration_seconds": 300,
        "total_cost": 0.02,
        "joules": 1200.0,
        "co2e_grams": 0.1,
        "node": "gpu-1",
        "cpu_request": 1000,
        "memory_request": 1024**3,
        "cost_source": "pricing_catalog",
        "model_version": IMPACT_MODEL_VERSION,
        "gpu_power_watts": 210.5,
        "gpu_usage_millicores": 800,
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _idle(timestamp: datetime, **overrides: Any) -> NodeIdleMetric:
    values: dict[str, Any] = {
        "node": "gpu-1",
        "timestamp": timestamp,
        "duration_seconds": 300,
        "joules": 900.0,
        "co2e_grams": 0.05,
        "embodied_co2e_grams": 0.4,
        "total_cost": 0.01,
        "cost_cpu": 0.008,
        "cost_ram": 0.002,
        "cost_source": "pricing_catalog",
        "cpu_unreserved_cores": 3.0,
        "memory_unreserved_bytes": 12.0 * 1024**3,
        "gpu_power_watts": 50.0,
        "pue": 1.2,
        "grid_intensity": 60.0,
        "emaps_zone": "FR",
        "power_source": "instance_profile",
        "intensity_source": "realtime",
    }
    values.update(overrides)
    return NodeIdleMetric(**values)


@pytest.mark.asyncio
async def test_pod_and_container_rows_keep_model_version_and_gpu_power(repo):
    await repo.write_combined_metrics([_row(T0), _row(T0, container_name="trainer")])

    (pod,) = await repo.read_combined_metrics(T0, T0 + timedelta(minutes=5))
    (container,) = await repo.read_container_metrics(T0, T0 + timedelta(minutes=5))
    for row in (pod, container):
        assert row.model_version == IMPACT_MODEL_VERSION
        assert row.gpu_power_watts == pytest.approx(210.5)
    assert pod.gpu_usage_millicores == 800

    # Recompute (B8) rewrites a row in place, including its model version.
    await repo.write_combined_metrics([_row(T0, joules=600.0, model_version="9.9.9")])
    (pod,) = await repo.read_combined_metrics(T0, T0 + timedelta(minutes=5))
    assert pod.joules == pytest.approx(600.0)
    assert pod.model_version == "9.9.9"


@pytest.mark.asyncio
async def test_rows_without_model_version_read_back_as_none(repo):
    await repo.write_combined_metrics([_row(T0, model_version=None, gpu_power_watts=None)])
    (pod,) = await repo.read_combined_metrics(T0, T0 + timedelta(minutes=5))
    assert pod.model_version is None
    assert pod.gpu_power_watts is None


@pytest.mark.asyncio
async def test_idle_rows_upsert_per_node_and_step(repo):
    later = T0 + timedelta(minutes=5)
    written = await repo.write_idle_metrics([_idle(T0), _idle(later), _idle(T0, node="cpu-1", gpu_power_watts=None)])
    assert written == 3
    # Re-collecting a window replaces its idle rows instead of duplicating them.
    await repo.write_idle_metrics([_idle(T0, joules=450.0)])

    rows = await repo.read_idle_metrics(T0, T0 + timedelta(minutes=10))
    assert [(row.node, row.timestamp) for row in rows] == [("cpu-1", T0), ("gpu-1", T0), ("gpu-1", later)]
    gpu_t0 = rows[1]
    assert gpu_t0.joules == pytest.approx(450.0)
    assert gpu_t0.scope == "idle"
    assert gpu_t0.model_version == IMPACT_MODEL_VERSION
    assert gpu_t0.memory_unreserved_bytes == pytest.approx(12.0 * 1024**3)
    assert gpu_t0.timestamp.tzinfo is not None
    assert rows[0].gpu_power_watts is None
    # Half-open range.
    assert await repo.read_idle_metrics(later, later) == []
    assert len(await repo.read_idle_metrics(later, later + timedelta(minutes=5))) == 1


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``T0`` for each test: a wall-clock constant taken at import ages during a long suite."""
    monkeypatch.setitem(
        globals(), "T0", datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    )


async def test_pod_container_and_idle_rows_keep_gpu_source(repo):
    """NRG-13: where the GPU power of a row came from survives storage."""
    await repo.write_combined_metrics(
        [_row(T0, gpu_source="gpu_tdp_estimate"), _row(T0, container_name="trainer", gpu_source="gpu_tdp_estimate")]
    )
    await repo.write_idle_metrics([_idle(T0, gpu_source="unmeasured")])
    (pod,) = await repo.read_combined_metrics(T0, T0 + timedelta(minutes=5))
    (container,) = await repo.read_container_metrics(T0, T0 + timedelta(minutes=5))
    (idle,) = await repo.read_idle_metrics(T0, T0 + timedelta(minutes=5))
    assert (pod.gpu_source, container.gpu_source, idle.gpu_source) == (
        "gpu_tdp_estimate",
        "gpu_tdp_estimate",
        "unmeasured",
    )
