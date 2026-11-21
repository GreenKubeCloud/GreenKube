"""``greenkube recompute --all`` gives what the collection gave (NRG-12, NRG-13, NRG-15).

Collection splits a shared host, charges the CPU used outside pods to the idle row and
estimates GPUs without DCGM. Re-allocating the stored rows must give the same energy,
CO2e, embodied CO2e and reasons, on SQLite and PostgreSQL.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.historical_range_processor import HistoricalRangeProcessor
from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.energy.node_power import default_profile_from_config, resolve_node_power_profile
from greenkube.energy.shared_host import shared_host_groups
from greenkube.impact.power import undetected_gpu_power
from greenkube.impact.recompute import MetricRecomputer
from greenkube.impact.rows import MetricRowAllocator, NodeIdleMetric
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

GIB = 1024**3
STEP = 300


def _t0() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def stores(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_recparity_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "parity.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        if manager.db_type == "postgres":
            yield PostgresCombinedMetricsRepository(manager), PostgresNodeRepository(manager)
        else:
            yield SQLiteCombinedMetricsRepository(manager), SQLiteNodeRepository(manager)
    finally:
        await manager.close()
        if backend == "postgres" and dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _kind_node(name: str, t0: datetime, **overrides: Any) -> NodeInfo:
    values: Dict[str, Any] = {
        "name": name,
        "cloud_provider": "kind",
        "cpu_capacity_cores": 14,
        "memory_capacity_bytes": 39 * GIB,
        "host_id": "boot-1",
        "provider_id": f"kind://docker/kind/{name}",
        "timestamp": t0 - timedelta(hours=1),
    }
    values.update(overrides)
    return NodeInfo(**values)


def _row(node: str, t0: datetime, **overrides: Any) -> CombinedMetric:
    values: Dict[str, Any] = {
        "pod_name": f"p-{node}",
        "namespace": "ns",
        "node": node,
        "timestamp": t0,
        "duration_seconds": STEP,
        "cpu_request": 500,
        "cpu_usage_millicores": 500,
        "memory_request": GIB,
        "memory_usage_bytes": GIB,
        "pue": 1.0,
        "grid_intensity": 100.0,
        "emaps_zone": "FR",
        "intensity_source": "realtime",
        "joules": 0.0,
        "co2e_grams": 0.0,
        "embodied_co2e_grams": 0.0,
        "total_cost": 0.0,
        "cost_source": "none",
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _collect(cfg: Config, nodes: Dict[str, NodeInfo], t0: datetime, *, node_cpu: float | None = 2.0, gpu=None):
    """Allocate like ``run_range`` does: shared host share, root-cgroup CPU, undetected GPUs."""
    allocator = MetricRowAllocator(cfg)
    groups = shared_host_groups(nodes)
    rows_out: List[CombinedMetric] = []
    idle_out: List[NodeIdleMetric] = []
    for name, info in nodes.items():
        power = resolve_node_power_profile(
            node_name=name,
            instance_type=None,
            cpu_capacity_cores=info.cpu_capacity_cores,
            provider=info.cloud_provider,
            instance_profiles=INSTANCE_PROFILES,
            default_profile=default_profile_from_config(cfg),
        )
        spec = HistoricalRangeProcessor._node_spec(
            name,
            info,
            power,
            {},
            instance_type=None,
            gpu_unattributed_watts=(gpu.watts or 0.0) if gpu is not None else 0.0,
            config=cfg,
            shared_host=groups.get(name),
            undetected_gpu=gpu,
            gpu_source=gpu.source if gpu is not None else None,
            node_cpu_used_cores=node_cpu,
        )
        rows = [_row(name, t0)]
        profile = allocator.node_profile(spec, rows, pue=1.0, intensity=100.0, hours=STEP / 3600)
        allocated = allocator.allocate(
            rows,
            profile,
            STEP / 3600,
            idle_fields={"timestamp": t0, "emaps_zone": "FR", "intensity_source": "realtime"},
        )
        assert allocated.idle is not None
        rows_out.extend(allocated.rows)
        idle_out.append(allocated.idle)
    return rows_out, idle_out


def _totals(rows, idle) -> tuple[float, float, float]:
    return (
        sum(r.joules for r in rows) + sum(i.joules for i in idle),
        sum(r.co2e_grams for r in rows) + sum(i.co2e_grams for i in idle),
        sum(r.embodied_co2e_grams or 0.0 for r in rows) + sum(i.embodied_co2e_grams for i in idle),
    )


async def _store_and_recompute(stores, cfg, nodes, rows, idle, t0):
    repo, node_repo = stores
    await node_repo.save_nodes(list(nodes.values()))
    await repo.write_combined_metrics(rows)
    await repo.write_idle_metrics(idle)
    report = await MetricRecomputer(cfg, repo, node_repo).run(
        t0 - timedelta(minutes=1), t0 + timedelta(minutes=10), only_outdated=False
    )
    after_rows = [r for r in await repo.read_combined_metrics(t0, t0 + timedelta(minutes=5)) if not r.container_name]
    after_idle = await repo.read_idle_metrics(t0, t0 + timedelta(minutes=5))
    return report, after_rows, after_idle


def _by_node(rows):
    return {row.node: row for row in rows}


async def test_shared_host_survives_recompute(stores):
    t0 = _t0()
    cfg = Config()
    nodes = {n: _kind_node(n, t0) for n in ("kind-control-plane", "kind-worker", "kind-worker2")}
    rows, idle = _collect(cfg, nodes, t0)
    report, after_rows, after_idle = await _store_and_recompute(stores, cfg, nodes, rows, idle, t0)
    assert report.pod_rows_recomputed == 3
    assert _totals(after_rows, after_idle) == pytest.approx(_totals(rows, idle))
    before = _by_node(rows)
    for name, row in _by_node(after_rows).items():
        assert row.joules == pytest.approx(before[name].joules)
        assert row.estimation_reasons == before[name].estimation_reasons
        assert any(r.startswith("Shared host") for r in row.estimation_reasons)


async def test_cpu_used_outside_pods_survives_recompute(stores):
    t0 = _t0()
    cfg = Config()
    nodes = {"n1": _kind_node("n1", t0, host_id=None, provider_id=None)}
    rows, idle = _collect(cfg, nodes, t0)
    _, after_rows, after_idle = await _store_and_recompute(stores, cfg, nodes, rows, idle, t0)
    assert after_idle[0].joules == pytest.approx(idle[0].joules)
    assert after_rows[0].joules == pytest.approx(rows[0].joules)


async def test_gpu_estimate_survives_recompute(stores):
    t0 = _t0()
    cfg = Config()
    nodes = {"g1": _kind_node("g1", t0, host_id=None, provider_id=None)}
    rows, idle = _collect(cfg, nodes, t0, node_cpu=None, gpu=undetected_gpu_power(4, "Tesla-T4"))
    _, after_rows, after_idle = await _store_and_recompute(stores, cfg, nodes, rows, idle, t0)
    assert after_idle[0].gpu_source == "gpu_tdp_estimate"
    assert after_idle[0].joules == pytest.approx(idle[0].joules)
    assert after_rows[0].gpu_source == "gpu_tdp_estimate"
    assert after_rows[0].estimation_reasons == rows[0].estimation_reasons


async def test_current_rows_without_recorded_inputs_are_left_unchanged(stores):
    """Rows of the current model whose idle row predates 0184 cannot be re-derived: they stay as stored."""
    t0 = _t0()
    cfg = Config()
    nodes = {n: _kind_node(n, t0) for n in ("kind-control-plane", "kind-worker")}
    rows, idle = _collect(cfg, nodes, t0)
    legacy_idle = [replace(i, host_share=None, node_cpu_used_cores=None, joules=1.0) for i in idle]
    report, after_rows, after_idle = await _store_and_recompute(stores, cfg, nodes, rows, legacy_idle, t0)
    assert report.pod_rows_recomputed == 0
    assert report.groups_skipped_unrecorded == 2
    assert {i.joules for i in after_idle} == {1.0}


async def test_older_rows_take_the_shared_host_from_the_snapshots(stores):
    """3.0.0 rows (no idle inputs) on a kind cluster get the host split from the stored host ids."""
    t0 = _t0()
    cfg = Config()
    nodes = {n: _kind_node(n, t0) for n in ("kind-control-plane", "kind-worker", "kind-worker2")}
    expected_rows, expected_idle = _collect(cfg, nodes, t0, node_cpu=None)
    old_rows = [_row(name, t0, model_version="3.0.0", joules=1.0) for name in nodes]
    _, after_rows, after_idle = await _store_and_recompute(stores, cfg, nodes, old_rows, [], t0)
    assert _totals(after_rows, after_idle) == pytest.approx(_totals(expected_rows, expected_idle))


async def test_a_dropped_node_reason_clears_is_estimated(stores):
    t0 = _t0()
    cfg = Config(SHARED_HOST_DETECTION=False)
    nodes = {"n1": _kind_node("n1", t0, host_id=None, provider_id=None, embodied_emissions_kg=500.0)}
    stale = _row(
        "n1",
        t0,
        model_version="3.0.0",
        cost_source="opencost",
        total_cost=0.01,
        is_estimated=True,
        estimation_reasons=["Shared host: 2 nodes share one host (bootID x); stale"],
    )
    _, after_rows, _ = await _store_and_recompute(stores, cfg, nodes, [stale], [], t0)
    assert after_rows[0].estimation_reasons == []
    assert after_rows[0].is_estimated is False
