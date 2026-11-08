"""Acceptance of memory-bounded scans: 500 pods x 14 days (roadmap E3, BUG-12).

The rollup of 500 pods over 14 days (336 hours x 1000 rows) is stored, then a
full native scan (context, analyzers, enrichment) runs in a fresh Python
process whose peak RSS and duration are measured: below 300 MB and 60 s on
SQLite and PostgreSQL. A fresh process keeps the measurement independent of
the test runner's own memory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.workload_usage import floor_hour
from greenkube.models.workload_usage import WorkloadUsageHour
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

MIB = 1024 * 1024
HOURS = 14 * 24
MAX_RSS_BYTES = 300 * 1024 * 1024
MAX_SECONDS = 60.0

SCAN_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, resource, sys, time
    from unittest.mock import AsyncMock, patch

    from greenkube.core.config import Config
    from greenkube.core.db import DatabaseManager
    from greenkube.core.optimization.context_builder import ContextBuilder
    from greenkube.core.optimization.dedup import finalize_recommendations
    from greenkube.core.optimization.enrich import enrich_recommendations
    from greenkube.core.optimization.providers.native import NativeSource
    from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

    SIDE = {"node_infos": [], "hpa_targets": set(), "persistent_volumes": [], "load_balancers": []}

    async def main():
        cfg = Config()
        manager = DatabaseManager(cfg)
        await manager.connect()
        started = time.perf_counter()
        with (
            patch("greenkube.core.optimization.context_builder.get_active_k8s_namespaces",
                  new=AsyncMock(return_value=None)),
            patch("greenkube.core.optimization.context_builder.ContextBuilder._collect_side_inputs",
                  new=AsyncMock(return_value=SIDE)),
        ):
            repo = create_workload_usage_repository(manager, manager.db_type)
            context = await ContextBuilder(cfg).build_from_rollup(repo, AsyncMock())
            recs = await NativeSource(cfg).collect(context)
            recs = enrich_recommendations(finalize_recommendations(recs, cfg), context, cfg)
        elapsed = time.perf_counter() - started
        await manager.close()
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        summaries = sum(len(c) for c in context.usage_snapshot().summaries.values())
        print(json.dumps({
            "seconds": elapsed,
            "peak_rss_bytes": peak_bytes,
            "recommendations": len(recs),
            "summaries": summaries,
            "baseline_rows": len(context.metrics) + len(context.container_metrics),
        }))

    asyncio.run(main())
    """
)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
async def scan_env(request, tmp_path, monkeypatch):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DB_") and key not in {"PYTEST_CURRENT_TEST", "VIRTUAL_ENV"}
    }
    env.update(
        {
            "PROMETHEUS_URL": "",
            "OPENCOST_API_URL": "",
            "LOG_LEVEL": "ERROR",
            "PROMETHEUS_QUERY_RANGE_STEP": "5m",
            "KUBERNETES_METRICS_API_ENABLED": "false",
        }
    )
    schema = None
    dsn = None
    if request.param == "postgres":
        dsn = os.getenv("GREENKUBE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set GREENKUBE_TEST_POSTGRES_DSN to run PostgreSQL scan acceptance tests.")
        schema = f"gk_perf_{uuid4().hex}"
        db_env = {
            "DB_TYPE": "postgres",
            "DB_CONNECTION_STRING": dsn,
            "DB_SCHEMA": schema,
            "DB_SSL_MODE": "disable",
            "DB_POOL_MIN_SIZE": "1",
            "DB_POOL_MAX_SIZE": "2",
        }
    else:
        db_env = {"DB_TYPE": "sqlite", "DB_PATH": str(tmp_path / "perf.sqlite")}
    for key, value in db_env.items():
        monkeypatch.setenv(key, value)
    env.update(db_env)
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager, env
    finally:
        await manager.close()
        if dsn and schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            finally:
                await conn.close()


def _targets() -> list[tuple[str, str, str, int]]:
    """60 Deployments x 5 replicas and 200 single-pod workloads: 500 pods."""
    targets = [(f"team-{i % 10}", "Deployment", f"service-{i}", 5) for i in range(60)]
    targets += [(f"team-{i % 10}", "StatefulSet", f"store-{i}", 1) for i in range(200)]
    return targets


def _hour_rows(hour: datetime, targets: list[tuple[str, str, str, int]]) -> list[WorkloadUsageHour]:
    rows: list[WorkloadUsageHour] = []
    for index, (namespace, kind, name, replicas) in enumerate(targets):
        base = 50.0 + (index % 7) * 40 + (hour.hour % 12) * 5
        for replica in range(replicas):
            node = f"node-{(index + replica * 7) % 20:02d}"
            for container in ("", "app"):
                scale = 1.0 if container == "" else 0.9
                rows.append(
                    WorkloadUsageHour(
                        hour_bucket=hour,
                        namespace=namespace,
                        target_kind=kind,
                        target_name=name,
                        container_name=container,
                        node=node,
                        hour_of_day=hour.hour,
                        sample_count=12,
                        pod_count=1,
                        step_count=12,
                        duration_seconds=3600.0,
                        cpu_sample_count=12,
                        cpu_usage_avg=base * scale,
                        cpu_usage_sq_avg=(base * scale) ** 2 * 1.1,
                        cpu_usage_p50=base * scale,
                        cpu_usage_p90=base * scale * 1.3,
                        cpu_usage_p95=base * scale * 1.4,
                        cpu_usage_p99=base * scale * 1.6,
                        cpu_usage_max=base * scale * 1.8,
                        memory_sample_count=12,
                        memory_usage_avg=200 * MIB * scale,
                        memory_usage_p50=200 * MIB * scale,
                        memory_usage_p90=220 * MIB * scale,
                        memory_usage_p95=230 * MIB * scale,
                        memory_usage_p99=240 * MIB * scale,
                        memory_usage_max=250 * MIB * scale,
                        cpu_request=1000,
                        memory_request=1024 * MIB,
                        cpu_request_sum=12000.0,
                        memory_request_sum=12.0 * 1024 * MIB,
                        cpu_limit=2000 if container else None,
                        memory_limit=2048 * MIB if container else None,
                        restart_count=0,
                        total_cost=0.05 * scale,
                        cost_cpu=0.03 * scale,
                        cost_ram=0.02 * scale,
                        cost_source="opencost",
                        co2e_grams=2.0 * scale,
                        joules=50_000.0 * scale,
                        pue=1.2,
                        grid_intensity=60.0,
                        intensity_sample_count=12,
                        emaps_zone="FR",
                        node_instance_type="m5.xlarge",
                    )
                )
    return rows


async def test_scan_of_500_pods_over_14_days_is_memory_and_time_bounded(scan_env, tmp_path):
    manager, env = scan_env
    repository = create_workload_usage_repository(manager, manager.db_type)
    targets = _targets()
    assert sum(replicas for *_, replicas in targets) == 500
    end = floor_hour(datetime.now(timezone.utc))
    for offset in range(HOURS, 0, -1):
        hour = end - timedelta(hours=offset)
        await repository.replace_hour(hour, _hour_rows(hour, targets))
    await manager.close()

    script = tmp_path / "scan.py"
    script.write_text(SCAN_SCRIPT)
    completed = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print(f"bounded scan ({manager.db_type}): {result}")

    # Every target container is summarized (260 targets x pod level + container).
    assert result["summaries"] == 2 * len(targets)
    assert result["recommendations"] > 0
    assert result["seconds"] < MAX_SECONDS, result
    assert result["peak_rss_bytes"] < MAX_RSS_BYTES, result
