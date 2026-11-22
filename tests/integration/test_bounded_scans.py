"""Memory-bounded recommendation scans on SQLite and PostgreSQL (roadmap E3, BUG-12).

Scans on the SQL backends read per-target usage summaries computed in SQL
from the ``workload_usage_hourly`` rollup, never the raw metric rows of the
lookback, and they produce the same recommendations as the row-based path.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.context_builder import ContextBuilder
from greenkube.core.optimization.dedup import finalize_recommendations
from greenkube.core.optimization.enrich import enrich_recommendations
from greenkube.core.optimization.liveness import TargetLiveness
from greenkube.core.optimization.providers.native import NativeSource
from greenkube.models.metrics import CombinedMetric, RecommendationType
from greenkube.models.workload_usage import BaselineMetric
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

MIB = 1024 * 1024


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def db(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    # Fixtures cover two days: the one-week decrease gate is tested elsewhere.
    monkeypatch.setenv("RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS", "0")
    # Likewise for the deletion unused period (test_deletion_safety.py).
    monkeypatch.setenv("DELETION_MIN_UNUSED_DAYS", "0")
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL scan tests.")
        schema = f"gk_scan_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "scan.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


@pytest.fixture(autouse=True)
def no_cluster():
    """No Kubernetes access: namespaces and side inputs are unavailable."""
    with (
        patch(
            "greenkube.core.optimization.context_builder.get_active_k8s_namespaces",
            new=AsyncMock(return_value=None),
        ) as namespaces,
        patch(
            # The data ends hours before the scan and no pod list is readable: liveness (REC-05) would
            # leave every target out; these tests are about bounded reads, not liveness.
            "greenkube.core.optimization.context_builder.ContextBuilder._filter_live_targets",
            new=AsyncMock(return_value=TargetLiveness()),
        ),
        patch(
            "greenkube.core.optimization.context_builder.ContextBuilder._collect_side_inputs",
            new=AsyncMock(
                return_value={"node_infos": [], "hpa_targets": set(), "persistent_volumes": [], "load_balancers": []}
            ),
        ),
    ):
        yield namespaces


def _repo(manager: DatabaseManager):
    if manager.db_type == "postgres":
        return PostgresCombinedMetricsRepository(manager)
    return SQLiteCombinedMetricsRepository(manager)


def _rows(start: datetime, hours: int) -> list[CombinedMetric]:
    """``api``: 2 replicas, oversized CPU and memory; ``hot``: CPU above its request; ``idle-ns``: an idle workload."""
    rows: list[CombinedMetric] = []
    for step in range(hours * 12):
        ts = start + timedelta(minutes=5 * step)
        wave = (step % 12) * 5
        for replica, node in (("api-7d9f8b6c5d-aaaaa", "node-a"), ("api-7d9f8b6c5d-bbbbb", "node-b")):
            common: dict[str, Any] = {
                "pod_name": replica,
                "namespace": "shop",
                "owner_kind": "Deployment",
                "owner_name": "api",
                "timestamp": ts,
                "duration_seconds": 300,
                "memory_request": 1024 * MIB,
                "cost_source": "opencost",
                "emaps_zone": "FR",
                "grid_intensity": 40.0,
                "pue": 1.2,
                "joules": 3000.0,
            }
            rows.append(
                CombinedMetric(
                    restart_count=0,
                    **common,
                    node=node,
                    cpu_request=1000,
                    cpu_usage_millicores=60 + wave,
                    memory_usage_bytes=180 * MIB,
                    total_cost=0.004,
                    cost_cpu=0.003,
                    cost_ram=0.001,
                    co2e_grams=0.2,
                )
            )
            rows.append(
                CombinedMetric(
                    restart_count=0,
                    **common,
                    container_name="web",
                    cpu_request=1000,
                    cpu_limit=2000,
                    cpu_usage_millicores=55 + wave,
                    memory_usage_bytes=170 * MIB,
                    total_cost=0.004,
                    cost_cpu=0.003,
                    cost_ram=0.001,
                    co2e_grams=0.2,
                )
            )
        hot: dict[str, Any] = {
            "pod_name": "hot-0",
            "namespace": "shop",
            "owner_kind": "StatefulSet",
            "owner_name": "hot",
            "timestamp": ts,
            "duration_seconds": 300,
            "cpu_request": 200,
            "memory_request": 512 * MIB,
            "cpu_usage_millicores": 190 + (step % 3) * 10,
            "memory_usage_bytes": 100 * MIB,
            "total_cost": 0.002,
            "cost_cpu": 0.0015,
            "cost_ram": 0.0005,
            "cost_source": "opencost",
            "joules": 2000.0,
        }
        rows.append(CombinedMetric(restart_count=0, **hot, node="node-a"))
        # Increases need container-level usage (pod-level usage never sizes a container).
        rows.append(CombinedMetric(restart_count=0, **hot, container_name="db"))
        rows.append(
            CombinedMetric(
                restart_count=0,
                pod_name="forgotten",
                namespace="idle-ns",
                timestamp=ts,
                duration_seconds=300,
                node="node-b",
                cpu_request=100,
                memory_request=128 * MIB,
                cpu_usage_millicores=0,
                memory_usage_bytes=10 * MIB,
                total_cost=0.01,
                joules=0.1,
            )
        )
    return rows


def _start() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)


async def _generate(context, cfg: Config):
    recs = await NativeSource(cfg).collect(context)
    return enrich_recommendations(finalize_recommendations(recs, cfg), context, cfg)


def _key(rec):
    return (rec.type, rec.namespace, rec.owner_name or rec.pod_name, rec.container_name)


async def test_scan_never_materializes_raw_rows(db):
    """BUG-12: the SQL scan path reads summaries and bounded baseline rows, not the lookback's rows."""
    repo = _repo(db)
    start = _start()
    await repo.write_combined_metrics(_rows(start, hours=36))
    cfg = Config()

    with (
        patch.object(type(repo), "read_combined_metrics_smart", side_effect=AssertionError("raw rows loaded")),
        patch.object(type(repo), "read_container_metrics_smart", side_effect=AssertionError("raw rows loaded")),
    ):
        context = await ContextBuilder(cfg).build(repo, AsyncMock())

    assert context.usage is not None and context.usage.source == "rollup"
    assert all(isinstance(row, BaselineMetric) for row in (*context.metrics, *context.container_metrics))
    # Pod-level rows: at most one per target, node and hour of day (here 3 targets, 2 nodes for api).
    assert len(context.metrics) <= 4 * 24
    assert len(context.container_metrics) <= 3
    # The window is annualized over the observed span (first hour to now), not the 14-day lookback.
    assert context.analysis_window_seconds is not None
    observed = (datetime.now(timezone.utc) - start).total_seconds()
    assert context.analysis_window_seconds == pytest.approx(observed, abs=120)
    assert context.window_start == start

    recs = await _generate(context, cfg)
    by_key = {_key(rec): rec for rec in recs}
    cpu = by_key[(RecommendationType.RIGHTSIZING_CPU, "shop", "api", "web")]
    assert cpu.recommended_cpu_request_millicores is not None and cpu.recommended_cpu_request_millicores < 1000
    assert cpu.potential_savings_cost is not None and cpu.potential_savings_cost > 0
    assert cpu.evidence is not None and cpu.evidence.current.cpu_request_millicores == 1000
    assert cpu.patch is not None
    hot = by_key[(RecommendationType.RIGHTSIZING_CPU, "shop", "hot", "db")]
    assert hot.recommended_cpu_request_millicores is not None and hot.recommended_cpu_request_millicores > 200
    assert hot.potential_savings_cost is not None and hot.potential_savings_cost < 0


async def test_rollup_and_row_paths_agree(db):
    """SQL summaries over the rollup and Python summaries over the same rows give the same advice."""
    repo = _repo(db)
    rows = _rows(_start(), hours=30)
    await repo.write_combined_metrics(rows)
    cfg = Config()

    rollup_context = await ContextBuilder(cfg).build(repo, AsyncMock())
    rows_context = await ContextBuilder(cfg).build_from_metrics(
        rows, AsyncMock(), analysis_window_seconds=rollup_context.analysis_window_seconds
    )

    from_rollup = {_key(rec): rec for rec in await _generate(rollup_context, cfg)}
    from_rows = {_key(rec): rec for rec in await _generate(rows_context, cfg)}

    assert set(from_rollup) == set(from_rows)
    for key, rec in from_rollup.items():
        other = from_rows[key]
        assert rec.recommended_cpu_request_millicores == other.recommended_cpu_request_millicores, key
        assert rec.recommended_memory_request_bytes == other.recommended_memory_request_bytes, key
        if other.potential_savings_cost:
            # Baseline rows average usage within a group: exact while usage stays below the
            # request, approximate (here 8%) when per-step usage crosses it, as for ``hot``.
            tolerance = 0.02 if other.potential_savings_cost > 0 else 0.1
            assert rec.potential_savings_cost == pytest.approx(other.potential_savings_cost, rel=tolerance), key
        if rec.evidence is not None and other.evidence is not None:
            assert rec.evidence.sample_count == other.evidence.sample_count, key


async def test_namespace_scope_and_inactive_namespaces(db, no_cluster):
    repo = _repo(db)
    await repo.write_combined_metrics(_rows(_start(), hours=6))
    cfg = Config()

    scoped = await ContextBuilder(cfg).build(repo, AsyncMock(), namespace="idle-ns")
    assert {target[0] for target in scoped.usage_snapshot().summaries} == {"idle-ns"}
    assert {row.namespace for row in scoped.metrics} == {"idle-ns"}

    no_cluster.return_value = {"shop"}
    active = await ContextBuilder(cfg).build(repo, AsyncMock())
    assert {target[0] for target in active.usage_snapshot().summaries} == {"shop"}
    assert {row.namespace for row in active.metrics} == {"shop"}


async def test_deleted_namespaces_leave_node_averages_and_observed_window(db, no_cluster):
    """History of a namespace that no longer exists counts nowhere in the scan, not only in summaries."""
    repo = _repo(db)
    start = _start()
    await repo.write_combined_metrics(_rows(start, hours=6))
    cfg = Config()
    no_cluster.return_value = {"shop", "idle-ns"}
    before = await ContextBuilder(cfg).build(repo, AsyncMock())

    # A deleted namespace ran a big pod on node-b, starting half a day earlier.
    gone_start = start - timedelta(hours=12)
    await repo.write_combined_metrics(
        [
            CombinedMetric(
                restart_count=0,
                pod_name="old-0",
                namespace="gone",
                timestamp=gone_start + timedelta(minutes=5 * step),
                duration_seconds=300,
                node="node-b",
                cpu_request=4000,
                memory_request=8192 * MIB,
                cpu_usage_millicores=3000,
                memory_usage_bytes=4096 * MIB,
                total_cost=0.05,
            )
            for step in range(18 * 12)
        ]
    )
    after = await ContextBuilder(cfg).build(repo, AsyncMock())

    assert "gone" not in {target[0] for target in after.usage_snapshot().summaries}
    assert after.window_start == before.window_start
    assert after.analysis_window_seconds == pytest.approx(before.analysis_window_seconds, abs=120)
