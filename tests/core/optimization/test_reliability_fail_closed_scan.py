"""Scan-level regression of REC-04: a failed OOM query never turns into a decrease.

The OOM query fails (``WorkloadHealthCollector._query`` returns None on an
HTTP 503); the rollup shows the container's pod restarting. The scan must emit
no decrease, report the withheld decrease on the source run, and keep the
previously active memory increase (incomplete source, no stale).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import RELIABILITY_SIGNALS_UNKNOWN
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.reliability_signals import attach_reliability_signals
from greenkube.core.workload_usage import rollup_rows, summarize
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import SourceRunStatus
from greenkube.signals.adapters import analyzer_source_class
from greenkube.signals.sdk import SourceDeps, SourceSettings
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

MIB = 1024 * 1024
END = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
TARGET = ("shop", "Deployment", "api")


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "rec04.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


def _snapshot():
    """14 days of a low-usage container whose pod restart count grows (pod rows only carry it)."""
    t0 = END - timedelta(days=14)
    pods, containers = [], []
    for i in range(14 * 288):
        base: Dict[str, Any] = dict(
            namespace="shop",
            pod_name="api-7c9d8f6b5d-abcde",
            timestamp=t0 + timedelta(minutes=5 * i),
            owner_kind="Deployment",
            owner_name="api",
            node="n1",
            sample_count=1,
            duration_seconds=300,
            cpu_usage_millicores=20,
            cpu_usage_max_millicores=None,
            memory_usage_bytes=100 * MIB,
            memory_usage_max_bytes=None,
            cpu_request=1000,
            memory_request=1024 * MIB,
            cpu_limit=None,
            memory_limit=1024 * MIB,
            total_cost=0.0,
            co2e_grams=0.0,
            embodied_co2e_grams=0.0,
            joules=0.0,
        )
        pods.append(NS(restart_count=i // 110, **base))
        containers.append(NS(container_name="app", restart_count=None, **base))
    snapshot = summarize(rollup_rows(pods, containers, step_seconds=300, warmup_seconds=600))
    snapshot.source = "rollup"
    return snapshot


class _Prometheus503:
    """kube-state-metrics is scraped, but the OOM and restart queries fail (HTTP 503 -> None)."""

    def __init__(self) -> None:
        self.queries: List[str] = []

    async def __call__(self, promql: str, at: datetime) -> Optional[List[Dict[str, Any]]]:
        self.queries.append(promql)
        if promql.startswith("count(kube_pod_container_status_restarts_total)"):
            return [{"metric": {}, "value": [at.timestamp(), "5"]}]
        if "kube_pod_container_status_restarts_total" in promql or "container_oom_events_total" in promql:
            return None
        return []


async def _no_pods() -> List[Any]:
    return []


async def test_failed_oom_query_keeps_the_increase_and_reports_the_withheld_decrease(sqlite_db):
    repo = SQLiteRecommendationRepository(sqlite_db)
    increase = Recommendation(
        pod_name="api",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="OOM-driven increase",
        current_memory_request_bytes=512 * MIB,
        recommended_memory_request_bytes=1024 * MIB,
    )
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(increase)])

    snapshot = _snapshot()
    await attach_reliability_signals(
        snapshot, get_config(), start=END - timedelta(days=14), end=END, query=_Prometheus503(), list_pods=_no_pods
    )
    summary = snapshot.summaries[TARGET]["app"]
    assert summary.oom_kill_count is None and summary.restarts_in_window is None
    assert snapshot.summaries[TARGET][""].restart_increase

    sources = [analyzer_source_class(MemoryRightsizingAnalyzer), analyzer_source_class(CpuRightsizingAnalyzer)]
    deps = SourceDeps(config=get_config(), cluster_uid="c")
    engine = OptimizationEngine(sources=[cls(SourceSettings(), deps) for cls in sources])
    engine.build_context = AsyncMock(
        return_value=OptimizationContext(config=get_config(), usage=snapshot, analysis_window_seconds=14 * 86400)
    )

    result = await engine.run_scan(AsyncMock(), AsyncMock(), repo)

    assert not [rec for rec in result.recommendations if rec.type == RecommendationType.RIGHTSIZING_CPU]
    memory = [rec for rec in result.recommendations if rec.type == RecommendationType.RIGHTSIZING_MEMORY]
    assert all((rec.recommended_memory_request_bytes or 0) >= 1024 * MIB for rec in memory)

    statuses = {outcome.source: outcome.status for outcome in result.source_outcomes}
    assert SourceRunStatus.INCOMPLETE in statuses.values()
    reports = {report.source_id: report for report in result.source_reports}
    memory_run = reports["greenkube.rightsizing_memory"].run
    [diagnostic] = [d for d in memory_run.diagnostics if d.code == RELIABILITY_SIGNALS_UNKNOWN]
    assert "OOM kills failed" in diagnostic.message and "restarts failed" in diagnostic.message

    active = await repo.get_active_recommendations()
    kept = [rec for rec in active if rec.type == RecommendationType.RIGHTSIZING_MEMORY and rec.container_name == "app"]
    assert len(kept) == 1
    assert kept[0].recommended_memory_request_bytes == 1024 * MIB


async def test_an_incomplete_rightsizing_source_never_keeps_a_previous_decrease_alive(sqlite_db):
    """The withheld decrease must not survive as last-known advice: only increases are kept."""
    repo = SQLiteRecommendationRepository(sqlite_db)
    decrease = Recommendation(
        pod_name="api",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="decrease emitted while the signals failed open",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=100,
    )
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(decrease)])

    snapshot = _snapshot()
    await attach_reliability_signals(
        snapshot, get_config(), start=END - timedelta(days=14), end=END, query=_Prometheus503(), list_pods=_no_pods
    )
    deps = SourceDeps(config=get_config(), cluster_uid="c")
    engine = OptimizationEngine(sources=[analyzer_source_class(CpuRightsizingAnalyzer)(SourceSettings(), deps)])
    engine.build_context = AsyncMock(
        return_value=OptimizationContext(config=get_config(), usage=snapshot, analysis_window_seconds=14 * 86400)
    )

    await engine.run_scan(AsyncMock(), AsyncMock(), repo)

    active = await repo.get_active_recommendations()
    assert not [rec for rec in active if rec.type == RecommendationType.RIGHTSIZING_CPU]
