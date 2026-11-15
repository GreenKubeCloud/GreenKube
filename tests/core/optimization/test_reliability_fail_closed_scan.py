"""Scan-level regression of REC-04: a failed OOM query never turns into a decrease.

The OOM and restart queries fail (``WorkloadHealthCollector._query`` returns
None on an HTTP 503). The usage snapshot is read the way production reads it:
metric rows are written to SQLite, rolled into ``workload_usage_hourly`` and
summarized in SQL (``read_usage_snapshot``).

* Without any stored restart count, the decreases are withheld: the source run
  reports it, the previous decrease is retired with the reason
  ``reliability_signals_unknown`` and the previous OOM-driven increase is
  kept, while every other record of the native source group still goes stale
  in the same scan (the group is not marked incomplete).
* With restarts stored in the rollup, the decreases are simply not
  recommended (no withheld diagnostic).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.core.config import Config, config, get_config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import RELIABILITY_SIGNALS_UNKNOWN
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.reliability_signals import attach_reliability_signals
from greenkube.core.workload_usage import WorkloadUsageRollup
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationRecord, RecommendationType
from greenkube.models.optimization_run import SourceRunStatus
from greenkube.signals.adapters import analyzer_source_class
from greenkube.signals.sdk import SourceDeps, SourceSettings
from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from greenkube.utils.time import to_iso_z

MIB = 1024 * 1024
END = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
DAYS = 8
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


async def _sql_snapshot(db: DatabaseManager, restarts: Callable[[int], Optional[int]], legacy_hour: bool = False):
    """Eight days of a low-usage container, written, rolled up and summarized in SQL.

    ``restarts(step)`` is the pod's restart count at each 5-minute step (pod rows
    only carry it; None: kube-state-metrics never scraped).
    """
    t0 = END - timedelta(days=DAYS)
    rows: List[CombinedMetric] = []
    for i in range(DAYS * 288):
        base: Dict[str, Any] = dict(
            namespace="shop",
            pod_name="api-7c9d8f6b5d-abcde",
            timestamp=t0 + timedelta(minutes=5 * i),
            owner_kind="Deployment",
            owner_name="api",
            duration_seconds=300,
            cpu_usage_millicores=20,
            memory_usage_bytes=100 * MIB,
            cpu_request=1000,
            memory_request=1024 * MIB,
            total_cost=0.001,
        )
        rows.append(CombinedMetric(node="n1", restart_count=restarts(i), **base))
        rows.append(CombinedMetric(container_name="app", memory_limit=1024 * MIB, **base))
    await SQLiteCombinedMetricsRepository(db).write_combined_metrics(rows)
    repository = create_workload_usage_repository(db, db.db_type)
    await WorkloadUsageRollup(repository, warmup_seconds=600).refresh(now=END + timedelta(minutes=30))
    if legacy_hour:
        # One hour rolled before migration 0130 whose raw rows are gone (upgrade case).
        hour = to_iso_z(t0 + timedelta(days=1))
        async with db.connection_scope() as conn:
            await conn.execute(
                "UPDATE workload_usage_hourly SET restart_increase = NULL WHERE hour_bucket = ?", (hour,)
            )
            await conn.execute(
                'DELETE FROM combined_metrics WHERE "timestamp" >= ? AND "timestamp" < ?',
                (hour, to_iso_z(t0 + timedelta(days=1, hours=1))),
            )
            await conn.commit()
        await WorkloadUsageRollup(repository, warmup_seconds=600).refresh(
            now=END + timedelta(minutes=30), refresh_hours=1
        )
    snapshot = await repository.read_usage_snapshot(t0, END + timedelta(hours=1))
    assert snapshot.source == "rollup"
    await attach_reliability_signals(
        snapshot, get_config(), start=t0, end=END, query=_Prometheus503(), list_pods=_no_pods
    )
    return snapshot


def _rightsizing(resource: str, current: int, proposed: int, description: str) -> Recommendation:
    fields: Dict[str, Any] = (
        {"current_cpu_request_millicores": current, "recommended_cpu_request_millicores": proposed}
        if resource == "cpu"
        else {"current_memory_request_bytes": current, "recommended_memory_request_bytes": proposed}
    )
    return Recommendation(
        pod_name="api",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_CPU if resource == "cpu" else RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description=description,
        **fields,
    )


def _engine(snapshot) -> OptimizationEngine:
    sources = [analyzer_source_class(MemoryRightsizingAnalyzer), analyzer_source_class(CpuRightsizingAnalyzer)]
    deps = SourceDeps(config=get_config(), cluster_uid="c")
    engine = OptimizationEngine(sources=[cls(SourceSettings(), deps) for cls in sources])
    engine.build_context = AsyncMock(
        return_value=OptimizationContext(config=get_config(), usage=snapshot, analysis_window_seconds=DAYS * 86400)
    )
    return engine


async def _active(repo: SQLiteRecommendationRepository) -> List[RecommendationRecord]:
    return await repo.get_active_recommendations()


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


async def test_unknown_history_withholds_decreases_without_freezing_the_native_group(sqlite_db):
    repo = SQLiteRecommendationRepository(sqlite_db)
    increase = _rightsizing("memory", 512 * MIB, 1024 * MIB, "OOM-driven increase")
    decrease = _rightsizing("cpu", 1000, 100, "decrease emitted while the signals failed open")
    # Another recommendation of the native group, no longer emitted: it must go stale in this scan.
    gone = Recommendation(
        pod_name="worker",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="worker",
        description="increase of a deleted workload",
        current_cpu_request_millicores=100,
        recommended_cpu_request_millicores=200,
    )
    zombie = Recommendation(
        pod_name="old-job",
        namespace="shop",
        type=RecommendationType.ZOMBIE_POD,
        scope="workload",
        owner_kind="Deployment",
        owner_name="old-job",
        description="appears idle",
    )
    await repo.upsert_recommendations(
        [RecommendationRecord.from_recommendation(rec) for rec in (increase, decrease, gone, zombie)]
    )
    before = {(rec.pod_name, rec.type): rec.id for rec in await _active(repo)}

    snapshot = await _sql_snapshot(sqlite_db, lambda step: None)
    summary = snapshot.summaries[TARGET]["app"]
    assert summary.oom_kill_count is None and summary.restarts_in_window is None
    assert snapshot.summaries[TARGET][""].restart_increase is None

    result = await _engine(snapshot).run_scan(AsyncMock(), AsyncMock(), repo)

    assert not [rec for rec in result.recommendations if rec.type == RecommendationType.RIGHTSIZING_CPU]
    memory = [rec for rec in result.recommendations if rec.type == RecommendationType.RIGHTSIZING_MEMORY]
    assert all((rec.recommended_memory_request_bytes or 0) >= 1024 * MIB for rec in memory)

    # The source runs are complete: one withheld target never freezes the group.
    assert {outcome.status for outcome in result.source_outcomes} == {SourceRunStatus.SUCCEEDED}
    reports = {report.source_id: report for report in result.source_reports}
    memory_run = reports["greenkube.rightsizing_memory"].run
    [diagnostic] = [d for d in memory_run.diagnostics if d.code == RELIABILITY_SIGNALS_UNKNOWN]
    assert "OOM kills failed" in diagnostic.message and "restarts failed" in diagnostic.message

    active = {(rec.pod_name, rec.type): rec for rec in await _active(repo)}
    kept = active.get(("api", RecommendationType.RIGHTSIZING_MEMORY))
    assert kept is not None and kept.recommended_memory_request_bytes == 1024 * MIB
    assert ("api", RecommendationType.RIGHTSIZING_CPU) not in active
    assert ("worker", RecommendationType.RIGHTSIZING_CPU) not in active
    assert ("old-job", RecommendationType.ZOMBIE_POD) not in active

    retired_id = before[("api", RecommendationType.RIGHTSIZING_CPU)]
    assert retired_id is not None
    events = await repo.get_events(retired_id)
    assert any(RELIABILITY_SIGNALS_UNKNOWN in str(event.payload) for event in events)


async def test_restarts_in_the_rollup_block_decreases_without_a_withheld_diagnostic(sqlite_db):
    repo = SQLiteRecommendationRepository(sqlite_db)
    increase = _rightsizing("memory", 512 * MIB, 1024 * MIB, "OOM-driven increase")
    await repo.upsert_recommendations([RecommendationRecord.from_recommendation(increase)])

    snapshot = await _sql_snapshot(sqlite_db, lambda step: step // 110)
    assert snapshot.summaries[TARGET][""].restart_increase

    result = await _engine(snapshot).run_scan(AsyncMock(), AsyncMock(), repo)

    decreases = [
        rec
        for rec in result.recommendations
        if (rec.recommended_cpu_request_millicores or 10**9) < (rec.current_cpu_request_millicores or 0)
        or (rec.recommended_memory_request_bytes or 10**12) < (rec.current_memory_request_bytes or 0)
    ]
    assert decreases == []
    for report in result.source_reports:
        assert not [d for d in report.run.diagnostics if d.code == RELIABILITY_SIGNALS_UNKNOWN]
    # The OOM kills are unknown (the restarts may be OOM kills): the increase is kept.
    active = await _active(repo)
    assert [rec for rec in active if rec.type == RecommendationType.RIGHTSIZING_MEMORY]


async def test_the_withheld_diagnostic_names_the_upgrade_on_the_sql_path(sqlite_db):
    """REC-04 review 3: history unknown only because of a pre-0130 hour says so (rollup + SQL summary)."""
    snapshot = await _sql_snapshot(sqlite_db, lambda step: 0, legacy_hour=True)
    assert snapshot.summaries[TARGET][""].restart_increase is None

    result = await _engine(snapshot).run_scan(AsyncMock(), AsyncMock(), SQLiteRecommendationRepository(sqlite_db))

    reports = {report.source_id: report for report in result.source_reports}
    [diagnostic] = [
        d for d in reports["greenkube.rightsizing_memory"].run.diagnostics if d.code == RELIABILITY_SIGNALS_UNKNOWN
    ]
    assert "because hours rolled before the upgrade (migration 0130)" in diagnostic.message
