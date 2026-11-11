"""A two-hour-old workload in a 14-day bounded scan is annualized over two hours (Impact Model 2.0.0).

E2E defect: the projector annualized every target over the scan's lookback, so
a workload observed for two hours inside a 14-day lookback was projected at
1/168 of its run-rate. The scan here goes through the real rollup and context
builder on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.workload_usage import floor_hour
from greenkube.impact import ImpactProjector
from greenkube.impact.types import HOURS_PER_YEAR
from greenkube.models.workload_usage import WorkloadUsageHour
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

GIB = 1024**3
LOOKBACK_HOURS = 14 * 24
HOURLY_COST = 0.05
SIDE = {"node_infos": [], "hpa_targets": set(), "persistent_volumes": [], "load_balancers": []}


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def database(request, tmp_path, monkeypatch):
    schema = None
    dsn = None
    monkeypatch.setenv("PROMETHEUS_QUERY_RANGE_STEP", "5m")
    monkeypatch.setenv("RECOMMENDATION_LOOKBACK_DAYS", "14")
    if request.param == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL scan tests.")
        schema = f"gk_coverage_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "coverage.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _hour(hour: datetime, name: str) -> WorkloadUsageHour:
    return WorkloadUsageHour(
        hour_bucket=hour,
        namespace="apps",
        target_kind="Deployment",
        target_name=name,
        container_name="",
        node="node-1",
        hour_of_day=hour.hour,
        sample_count=12,
        pod_count=1,
        step_count=12,
        duration_seconds=3600.0,
        cpu_sample_count=12,
        cpu_usage_avg=100.0,
        cpu_usage_sq_avg=100.0**2,
        cpu_usage_p50=100.0,
        cpu_usage_p90=100.0,
        cpu_usage_p95=100.0,
        cpu_usage_p99=100.0,
        cpu_usage_max=100.0,
        memory_sample_count=12,
        memory_usage_avg=GIB / 4,
        memory_usage_p50=GIB / 4,
        memory_usage_p90=GIB / 4,
        memory_usage_p95=GIB / 4,
        memory_usage_p99=GIB / 4,
        memory_usage_max=GIB / 4,
        cpu_request=500,
        memory_request=GIB,
        cpu_request_sum=12 * 500.0,
        memory_request_sum=12.0 * GIB,
        total_cost=HOURLY_COST,
        cost_cpu=HOURLY_COST * 0.7,
        cost_ram=HOURLY_COST * 0.3,
        cost_source="opencost",
        co2e_grams=2.0,
        joules=50_000.0,
        pue=1.2,
        grid_intensity=60.0,
        intensity_sample_count=12,
    )


async def test_young_workload_is_annualized_over_its_observed_hours(database):
    repository = create_workload_usage_repository(database, database.db_type)
    now = floor_hour(datetime.now(timezone.utc))
    for offset in range(LOOKBACK_HOURS, 0, -1):
        hour = now - timedelta(hours=offset)
        rows = [_hour(hour, "old")]
        if offset <= 2:
            rows.append(_hour(hour, "young"))
        await repository.replace_hour(hour, rows)

    with (
        patch(
            "greenkube.core.optimization.context_builder.get_active_k8s_namespaces",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "greenkube.core.optimization.context_builder.ContextBuilder._collect_side_inputs",
            new=AsyncMock(return_value=SIDE),
        ),
    ):
        context = await ContextBuilder(Config()).build_from_rollup(repository, AsyncMock(), now=now)

    assert context.analysis_window_seconds == pytest.approx(LOOKBACK_HOURS * 3600.0)
    projector = ImpactProjector.for_context(context)
    young = projector.workload_deletion(
        projector.workload_ref("apps", "Deployment", "young"), context.workload_rows(("apps", "Deployment", "young"))
    )
    old = projector.workload_deletion(
        projector.workload_ref("apps", "Deployment", "old"), context.workload_rows(("apps", "Deployment", "old"))
    )

    run_rate = HOURLY_COST * HOURS_PER_YEAR  # 438 per year for both workloads
    # Before the fix the young workload projected 2 x 0.05 x 8760 / 336 = 2.61 per year.
    assert young.allocated_cost_annual == pytest.approx(run_rate)
    assert old.allocated_cost_annual == pytest.approx(run_rate)
    assert young.assumptions["coverage_basis"] == "target_summary"
    assert young.assumptions["coverage_hours"] == pytest.approx(2.0)
    assert young.assumptions["short_coverage"] is True
    assert old.assumptions["coverage_hours"] == pytest.approx(LOOKBACK_HOURS)
    assert "short_coverage" not in old.assumptions
    assert young.confidence < old.confidence
