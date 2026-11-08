"""``greenkube recompute``: raw rows are re-allocated with the current Impact Model (roadmap B8)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from typer.testing import CliRunner

from greenkube.cli.main import app
from greenkube.cli.recompute import parse_since
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.impact import IMPACT_MODEL_VERSION
from greenkube.impact.recompute import MetricRecomputer, container_weights, split_to_containers
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.postgres.node_repository import PostgresNodeRepository
from greenkube.storage.postgres.repository import PostgresCombinedMetricsRepository
from greenkube.storage.sqlite.node_repository import SQLiteNodeRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository

GIB = 1024**3
T0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
STEP = 300
# m5.large: 2 vCPU, CCF 0.74 / 3.5 W per vCPU, 8 GiB, 100 kg embodied over 4 years.
IDLE_W, MAX_W = 1.48, 7.0


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def stores(request, tmp_path, monkeypatch):
    backend = request.param
    schema = None
    dsn = None
    if backend == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL storage tests.")
        schema = f"gk_recompute_{uuid4().hex}"
        monkeypatch.setenv("DB_TYPE", "postgres")
        monkeypatch.setenv("DB_CONNECTION_STRING", dsn)
        monkeypatch.setenv("DB_SCHEMA", schema)
        monkeypatch.setenv("DB_SSL_MODE", "disable")
        monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
        monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    else:
        monkeypatch.setenv("DB_TYPE", "sqlite")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "recompute.sqlite"))
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


def _pod(pod: str, cpu_request: int, cpu_usage: int, memory: int, **overrides: Any) -> CombinedMetric:
    values: dict[str, Any] = {
        "pod_name": pod,
        "namespace": "ns",
        "node": "n1",
        "node_instance_type": "m5.large",
        "timestamp": T0,
        "duration_seconds": STEP,
        "cpu_request": cpu_request,
        "cpu_usage_millicores": cpu_usage,
        "memory_request": memory,
        "memory_usage_bytes": memory,
        "pue": 1.2,
        "grid_intensity": 100.0,
        "emaps_zone": "FR",
        "intensity_source": "realtime",
        # Written by the previous pipeline: 100% of node power by usage share, no cost.
        "joules": 300.0,
        "co2e_grams": 0.01,
        "embodied_co2e_grams": 0.5,
        "total_cost": 0.0,
        "cost_source": "none",
        "model_version": None,
    }
    values.update(overrides)
    return CombinedMetric(**values)


def _container(pod: CombinedMetric, name: str, cpu_usage: int, memory: int) -> CombinedMetric:
    return pod.model_copy(
        update={
            "container_name": name,
            "cpu_usage_millicores": cpu_usage,
            "memory_usage_bytes": memory,
            "memory_request": memory,
        }
    )


async def _seed(repository, node_repository, **pod_overrides):
    await node_repository.save_nodes(
        [
            NodeInfo(
                name="n1",
                instance_type="m5.large",
                cloud_provider="aws",
                cpu_capacity_cores=2,
                memory_capacity_bytes=8 * GIB,
                embodied_emissions_kg=100.0,
                timestamp=T0 - timedelta(days=1),
            )
        ]
    )
    a = _pod("a", 1000, 500, 2 * GIB, **pod_overrides)
    b = _pod("b", 500, 100, 1 * GIB)
    await repository.write_combined_metrics(
        [a, b, _container(a, "app", 400, GIB), _container(a, "sidecar", 100, GIB), _container(b, "app", 100, GIB)]
    )


async def test_recompute_reallocates_outdated_rows_and_writes_idle(stores):
    repository, node_repository = stores
    await _seed(repository, node_repository)
    recomputer = MetricRecomputer(Config(), repository, node_repository)

    report = await recomputer.run(T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    assert report.pod_rows_recomputed == 2
    assert report.container_rows_recomputed == 3
    assert report.idle_rows_written == 1
    assert report.previous_versions == {None: 2}
    pods = {row.pod_name: row for row in await repository.read_combined_metrics(T0, T0 + timedelta(seconds=STEP))}
    (idle,) = await repository.read_idle_metrics(T0, T0 + timedelta(seconds=STEP))
    assert all(row.model_version == IMPACT_MODEL_VERSION for row in pods.values())

    # Conservation against the node totals: CPU profile + memory power, embodied, cost.
    node_watts = IDLE_W + 0.6 / 2 * (MAX_W - IDLE_W) + 8 * 0.392
    assert sum(row.joules for row in pods.values()) + idle.joules == pytest.approx(node_watts * STEP)
    embodied = 100 * 1000 / (4 * 8760) * STEP / 3600
    assert sum(row.embodied_co2e_grams or 0 for row in pods.values()) + idle.embodied_co2e_grams == pytest.approx(
        embodied
    )
    total_kwh = node_watts * STEP / 3.6e6
    assert sum(row.co2e_grams for row in pods.values()) + idle.co2e_grams == pytest.approx(total_kwh * 1.2 * 100)
    # Pod a reserves 1 of 2 cores: half the idle power and of the embodied emissions.
    assert pods["a"].embodied_co2e_grams == pytest.approx(embodied / 2)
    assert idle.cpu_unreserved_cores == pytest.approx(0.5)
    for row in pods.values():
        assert row.cost_source in {"pricing_catalog", "custom_rate"}
        assert row.total_cost > 0

    # Containers are re-split from their pod by CPU usage, like collection.
    containers = {
        (row.pod_name, row.container_name): row
        for row in await repository.read_container_metrics(T0, T0 + timedelta(seconds=STEP))
    }
    assert containers[("a", "app")].joules == pytest.approx(pods["a"].joules * 0.8)
    assert containers[("a", "sidecar")].joules == pytest.approx(pods["a"].joules * 0.2)
    assert containers[("a", "app")].model_version == IMPACT_MODEL_VERSION
    assert containers[("b", "app")].total_cost == pytest.approx(pods["b"].total_cost)

    # A second run finds nothing outdated.
    again = await recomputer.run(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert again.pod_rows_recomputed == 0
    assert again.groups_skipped_current == 1


async def test_dry_run_writes_nothing_and_all_recomputes_current_rows(stores):
    repository, node_repository = stores
    await _seed(repository, node_repository)
    recomputer = MetricRecomputer(Config(), repository, node_repository)

    report = await recomputer.run(T0, T0 + timedelta(hours=1), dry_run=True)
    assert report.pod_rows_recomputed == 2
    assert "would recompute" in report.summary()
    rows = await repository.read_combined_metrics(T0, T0 + timedelta(hours=1))
    assert all(row.model_version is None for row in rows)
    assert await repository.read_idle_metrics(T0, T0 + timedelta(hours=1)) == []

    await recomputer.run(T0, T0 + timedelta(hours=1))
    forced = await recomputer.run(T0, T0 + timedelta(hours=1), only_outdated=False)
    assert forced.pod_rows_recomputed == 2


async def test_observed_costs_are_kept(stores):
    repository, node_repository = stores
    await _seed(repository, node_repository, total_cost=0.004, cost_cpu=0.003, cost_ram=0.001, cost_source="opencost")

    await MetricRecomputer(Config(), repository, node_repository).run(T0, T0 + timedelta(hours=1))

    pods = {row.pod_name: row for row in await repository.read_combined_metrics(T0, T0 + timedelta(hours=1))}
    assert pods["a"].cost_source == "opencost"
    assert pods["a"].total_cost == pytest.approx(0.004)
    assert pods["b"].cost_source in {"pricing_catalog", "custom_rate"}


async def test_recompute_never_reads_modeled_costs_back_as_observed_prices(stores):
    """Modeled rows of a node with one OpenCost pod stay identical across recomputes (idempotence)."""
    repository, node_repository = stores
    await _seed(repository, node_repository, total_cost=1.0, cost_cpu=0.6, cost_ram=0.4, cost_source="opencost")
    # A second pod without OpenCost: modeled rows now outnumber the observed one.
    await repository.write_combined_metrics([_pod("c", 500, 100, 1 * GIB)])
    recomputer = MetricRecomputer(Config(), repository, node_repository)
    window = (T0, T0 + timedelta(seconds=STEP))

    await recomputer.run(T0, T0 + timedelta(hours=1))
    first = {row.pod_name: row for row in await repository.read_combined_metrics(*window)}
    (first_idle,) = await repository.read_idle_metrics(*window)
    await recomputer.run(T0, T0 + timedelta(hours=1), only_outdated=False)
    second = {row.pod_name: row for row in await repository.read_combined_metrics(*window)}
    (second_idle,) = await repository.read_idle_metrics(*window)

    assert second["a"].cost_source == "opencost" and second["a"].total_cost == pytest.approx(1.0)
    for pod in ("b", "c"):
        assert first[pod].cost_source in {"pricing_catalog", "custom_rate"}
        assert (second[pod].cost_source, second[pod].total_cost) == (
            first[pod].cost_source,
            pytest.approx(first[pod].total_cost),
        )
    # OpenCost idle cost is not queried: the idle row is modeled and labeled so.
    assert first_idle.cost_source in {"pricing_catalog", "custom_rate"}
    assert (second_idle.cost_source, second_idle.total_cost) == (
        first_idle.cost_source,
        pytest.approx(first_idle.total_cost),
    )


def test_split_to_containers_never_duplicates_pod_gpu_power():
    pod = _pod("a", 1000, 500, 2 * GIB, gpu_power_watts=60.0, gpu_usage_millicores=800)
    two = split_to_containers(pod, [_container(pod, "app", 400, GIB), _container(pod, "sidecar", 100, GIB)])
    assert all(row.gpu_power_watts is None and row.gpu_usage_millicores is None for row in two)
    (one,) = split_to_containers(pod, [_container(pod, "app", 400, GIB)])
    assert (one.gpu_power_watts, one.gpu_usage_millicores) == (60.0, 800)


def test_container_weights_fall_back_like_collection():
    base = CombinedMetric(pod_name="p", namespace="n", container_name="c")
    cpu, memory = container_weights(
        [base.model_copy(update={"cpu_request": 300}), base.model_copy(update={"cpu_request": 100})]
    )
    assert cpu == [300.0, 100.0]
    assert memory == cpu
    cpu, _ = container_weights([base, base])
    assert cpu == [1.0, 1.0]


def test_since_accepts_durations_and_timestamps():
    now = datetime.now(timezone.utc)
    assert parse_since("7d", now) == now - timedelta(days=7)
    stamp = (now - timedelta(days=2)).replace(microsecond=0)
    assert parse_since(stamp.isoformat(), now) == stamp


def test_command_is_registered():
    result = CliRunner().invoke(app, ["recompute", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--since" in plain
