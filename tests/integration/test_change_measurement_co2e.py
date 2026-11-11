"""Measured CO2e of a request change, compared component by component (Impact Model 2.0.0).

E2E defect: a realized rightsizing stored a measured CO2e of 0 while its
projection counted idle and embodied CO2e. Stored metric rows are allocated by
the Impact Model (idle power by reservation), so their operational CO2e moves
with requests: it is now measured and claimed when its own interval shows a
saving, split between the change's recommendations without double counting,
and compared with the projected components the rows carry (never with the
embodied component they do not carry).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

import os
from datetime import timedelta
from typing import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.impact.comparison import measurable_co2e_projection
from greenkube.models.metrics import RecommendationType
from greenkube.verify.model import MeasurementState, TimeWindow
from greenkube.verify.service import DECISION_GRACE
from greenkube.verify.strategy import HOURS_PER_YEAR
from tests.verify.fixtures import Scenario, record_status  # pyrefly: ignore[missing-import]


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch) -> AsyncGenerator[DatabaseManager, None]:
    backend = request.param
    postgres_dsn = None
    schema = None
    monkeypatch.setenv("ELECTRICITY_MAPS_TOKEN", "test-token")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
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
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "greenkube.sqlite"))
        monkeypatch.setenv("DB_SCHEMA", "public")

    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if postgres_dsn and schema:
            conn = await asyncpg.connect(postgres_dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _impact(idle: float, memory: float, embodied: float) -> dict:
    """A persisted projection as written before Impact Model 2.0.0 (breakdown only)."""
    return {
        "model_version": "1.1.0",
        "co2e_modeled": True,
        "realization_factor": 1.0,
        "co2e_breakdown_annual_g": {
            "idle_operational": idle,
            "dynamic_operational": 0.0,
            "memory_operational": memory,
            "gpu_operational": 0.0,
            "embodied": embodied,
            "total": idle + memory + embodied,
        },
    }


CPU_IMPACT = _impact(idle=20_000.0, memory=0.0, embodied=30_000.0)
MEMORY_IMPACT = _impact(idle=0.0, memory=5_000.0, embodied=0.0)


async def test_realized_rightsizing_claims_its_measured_operational_co2e(database):
    scenario = Scenario(database)
    completed = scenario.completed
    # Two replicas; the Impact Model allocates 12 g/h of operational CO2e per pod before
    # the change and 9 g/h after it (idle and memory power follow the reservation).
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed), co2e=12.0)
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)),
        cpu_request=300,
        memory_request=1024**3,
        co2e=9.0,
    )
    change, applied = await scenario.apply(
        fields={
            RecommendationType.RIGHTSIZING_CPU: {"impact": CPU_IMPACT, "potential_savings_co2e_grams": 50_000.0},
            RecommendationType.RIGHTSIZING_MEMORY: {"impact": MEMORY_IMPACT, "potential_savings_co2e_grams": 5_000.0},
        }
    )
    cpu_id, memory_id = (record.id for record in applied)
    assert cpu_id is not None and memory_id is not None

    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.state is MeasurementState.REALIZED

    measured_annual = 2 * 3.0 * HOURS_PER_YEAR  # 3 g/h saved on each of two pods
    records = {record.id: record for record in await scenario.records()}
    assert record_status(records[cpu_id]) == "verified" and record_status(records[memory_id]) == "verified"
    # Split by projected measurable CO2e (20,000 vs 5,000): never claimed twice.
    assert records[cpu_id].measured_co2e_saved_grams == pytest.approx(0.8 * measured_annual, rel=1e-6)
    assert records[memory_id].measured_co2e_saved_grams == pytest.approx(0.2 * measured_annual, rel=1e-6)

    rows = {row.recommendation_id: row for row in await scenario.measurements.list_for_change(change.change_id)}
    cpu_co2e = next(item for item in rows[cpu_id].estimates if item.quantity == "co2e_g")
    assert cpu_co2e.outcome.value == "realized"
    assert cpu_co2e.basis == "projected_co2e_share" and cpu_co2e.share == pytest.approx(0.8)
    # Compared with the idle share only: the 30,000 g of embodied CO2e are not in the measured rows.
    assert cpu_co2e.projected_annual == pytest.approx(20_000.0)
    assert cpu_co2e.projected_annual == pytest.approx(measurable_co2e_projection(CPU_IMPACT))
    assert cpu_co2e.projection_accuracy == pytest.approx(0.8 * measured_annual / 20_000.0, rel=1e-6)

    measurement = (await scenario.changes.list_measurements(change.change_id))[0]
    change_co2e = measurement.estimate("co2e_g")
    assert change_co2e is not None and change_co2e.projected_annual == pytest.approx(25_000.0)


async def test_unchanged_co2e_is_not_claimed(database):
    scenario = Scenario(database)
    completed = scenario.completed
    scenario.add_rows(TimeWindow(start=completed - timedelta(days=8), end=completed))
    scenario.add_rows(
        TimeWindow(start=completed, end=completed + timedelta(days=9)), cpu_request=300, memory_request=1024**3
    )
    change, applied = await scenario.apply(
        fields={
            RecommendationType.RIGHTSIZING_CPU: {"impact": CPU_IMPACT},
            RecommendationType.RIGHTSIZING_MEMORY: {"impact": MEMORY_IMPACT},
        }
    )
    await scenario.service.run_due(now=completed + timedelta(hours=2))
    status = await scenario.changes.get_status(change.change_id)
    assert status is not None and status.post_window_end is not None
    await scenario.service.run_due(now=status.post_window_end + DECISION_GRACE)
    records = await scenario.records()
    assert all(record_status(record) == "verified" for record in records)
    assert all(record.measured_co2e_saved_grams == 0.0 for record in records)
