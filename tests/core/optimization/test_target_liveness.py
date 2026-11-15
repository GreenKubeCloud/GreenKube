"""REC-05: recommendations of workloads that no longer run are retired, never re-emitted.

A target whose last sample is hours old and that has no running pod is left out
of the analyzers; its active recommendation becomes stale with the reason
``target_gone`` when the cluster confirms it is gone, and is kept (nothing new
emitted) when the controller still exists scaled to zero or when liveness could
not be read. Jobs and containers removed from the pod spec get no advice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.engine import OptimizationEngine, ReliabilityGuard
from greenkube.core.optimization.liveness import filter_live_targets
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

END = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
A = ("shop", "Deployment", "a")
B = ("shop", "Deployment", "b")
LIVE = ("shop", "Deployment", "live")
JOB = ("shop", "Job", "report-28391234")


def _summary(target, container: str, hours_ago: int) -> WorkloadUsageSummary:
    stats = UsageStats(sample_count=12, avg=100, p50=100, p90=100, p95=100, p99=100, max=100)
    return WorkloadUsageSummary(
        namespace=target[0],
        target_kind=target[1],
        target_name=target[2],
        container_name=container,
        first_hour=END - timedelta(days=10),
        last_hour=END - timedelta(hours=hours_ago),
        hours_observed=240,
        cpu=stats,
        memory=stats,
        cpu_request=1000,
        memory_request=1024,
    )


def _snapshot() -> UsageSnapshot:
    snapshot = UsageSnapshot(source="test")
    for target, hours_ago in ((A, 5), (B, 5), (LIVE, 0), (JOB, 0)):
        snapshot.summaries[target] = {"": _summary(target, "", hours_ago), "app": _summary(target, "app", hours_ago)}
    snapshot.summaries[LIVE]["removed"] = _summary(LIVE, "removed", 0)
    return snapshot


def test_gone_scaled_to_zero_jobs_and_removed_containers_are_left_out() -> None:
    snapshot = _snapshot()

    report = filter_live_targets(snapshot, end=END, live_containers={(LIVE, "app"), (JOB, "app")}, scaled_to_zero={B})

    assert set(snapshot.summaries) == {LIVE}
    assert set(snapshot.summaries[LIVE]) == {"", "app"}
    assert report.gone == {A, JOB}
    assert report.not_running == {B}
    assert report.removed_containers == {(LIVE, "removed")}


def test_unknown_liveness_never_retires_but_emits_nothing_for_stale_targets() -> None:
    snapshot = _snapshot()

    report = filter_live_targets(snapshot, end=END, live_containers=None, scaled_to_zero=None)

    assert set(snapshot.summaries) == {LIVE}
    assert report.gone == {JOB}
    assert report.not_running == {A, B}


def test_controllers_unreadable_keep_a_stale_deployment() -> None:
    snapshot = _snapshot()

    report = filter_live_targets(snapshot, end=END, live_containers={(LIVE, "app")}, scaled_to_zero=None)

    assert report.not_running == {A, B}
    assert report.gone == {JOB}


@pytest.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "liveness.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield SQLiteRecommendationRepository(manager)
    finally:
        await manager.close()


def _cpu(target) -> Recommendation:
    return Recommendation(
        pod_name=target[2],
        namespace=target[0],
        container_name="app",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind=target[1],
        owner_name=target[2],
        description="cpu rightsizing",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
    )


async def test_a_gone_target_is_retired_and_a_scaled_to_zero_one_is_kept(repo) -> None:
    engine = OptimizationEngine(sources=[])
    await engine._persist([_cpu(A), _cpu(B), _cpu(LIVE)], repo)

    guard = ReliabilityGuard(gone=frozenset({A}), not_running=frozenset({B}))
    await engine._persist([_cpu(LIVE)], repo, reliability=guard)

    active = {record.owner_name for record in await repo.get_active_recommendations()}
    assert active == {"b", "live"}
    [gone] = [r for r in await repo.get_recommendations_by_statuses(["stale"]) if r.owner_name == "a"]
    assert gone.id is not None
    events = await repo.get_events(gone.id)
    assert any(
        event.event_type == "stale" and getattr(event.payload, "reason", None) == "target_gone" for event in events
    )


def test_the_source_run_says_which_targets_were_left_out() -> None:
    from greenkube.core.optimization.analyzers.rightsizing_memory import liveness_diagnostics

    codes = {note["code"]: note["count"] for note in liveness_diagnostics(gone=2, not_running=1)}

    assert codes == {"target_gone": 2, "target_not_running": 1}
    assert liveness_diagnostics(0, 0) == []
