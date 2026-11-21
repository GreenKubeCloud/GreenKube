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
    # A running Job is never retired: it gets no advice, its records are kept.
    assert report.gone == {A}
    assert report.not_running == {B, JOB}
    assert report.removed_containers == {(LIVE, "removed")}


def test_unknown_liveness_never_retires_but_emits_nothing_for_stale_targets() -> None:
    snapshot = _snapshot()

    report = filter_live_targets(snapshot, end=END, live_containers=None, scaled_to_zero=None)

    assert set(snapshot.summaries) == {LIVE}
    assert report.gone == set()
    assert report.not_running == {A, B, JOB}


def test_controllers_unreadable_keep_a_stale_deployment() -> None:
    snapshot = _snapshot()

    report = filter_live_targets(snapshot, end=END, live_containers={(LIVE, "app")}, scaled_to_zero=None)

    assert report.not_running == {A, B}
    assert report.gone == {JOB}  # the pods were listed: a Job without a running pod finished


def test_a_daemonset_without_scheduled_pods_is_kept_like_a_scaled_to_zero_controller() -> None:
    daemonset = ("kube-system", "DaemonSet", "gpu-agent")
    snapshot = UsageSnapshot(source="test")
    snapshot.summaries[daemonset] = {"": _summary(daemonset, "", 5), "agent": _summary(daemonset, "agent", 5)}

    report = filter_live_targets(snapshot, end=END, live_containers=set(), scaled_to_zero=set())

    assert report.not_running == {daemonset}
    assert report.gone == set()


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


def test_owner_chain_transition_retires_the_stale_targets() -> None:
    """COL-16 review: stored rows of a Rollout's pods say Deployment/web and of a CronJob run Job/<run>;
    after the owner chain change those targets are retired as gone."""
    stale_deployment = ("shop", "Deployment", "web")
    rollout = ("shop", "Rollout", "web")
    old_run = ("shop", "Job", "report-28912345")
    snapshot = UsageSnapshot(source="test")
    for target, hours_ago in ((stale_deployment, 5), (rollout, 0), (old_run, 5)):
        snapshot.summaries[target] = {"": _summary(target, "", hours_ago)}

    report = filter_live_targets(snapshot, end=END, live_containers={(rollout, "app")}, scaled_to_zero=set())

    assert report.gone == {stale_deployment, old_run}
    assert set(snapshot.summaries) == {rollout}


ROLLOUT = ("shop", "Rollout", "web")
BARE_RS = ("shop", "ReplicaSet", "my-rs")
CRON = ("shop", "CronJob", "report")


def _stale_snapshot(*targets) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="test")
    for target in targets:
        snapshot.summaries[target] = {"": _summary(target, "", 5)}
    return snapshot


def test_rollouts_bare_replicasets_and_cronjobs_need_the_controller_check() -> None:
    """Re-review 1, 2: a stale Rollout, ReplicaSet or CronJob without pods triggers the controller lists."""
    from greenkube.core.optimization.liveness import needs_controller_check

    for target in (ROLLOUT, BARE_RS, CRON):
        assert needs_controller_check(_stale_snapshot(target), end=END, live_containers=set())


def test_present_rollouts_replicasets_and_cronjobs_are_kept_and_absent_ones_retired() -> None:
    """Re-review 1, 2: scaled to zero or present (CronJob, even suspended) is not_running; absent is gone."""
    snapshot = _stale_snapshot(ROLLOUT, BARE_RS, CRON)
    report = filter_live_targets(snapshot, end=END, live_containers=set(), scaled_to_zero={ROLLOUT, BARE_RS, CRON})
    assert report.not_running == {ROLLOUT, BARE_RS, CRON} and not report.gone

    snapshot = _stale_snapshot(ROLLOUT, BARE_RS, CRON)
    report = filter_live_targets(snapshot, end=END, live_containers=set(), scaled_to_zero=set())
    assert report.gone == {ROLLOUT, BARE_RS, CRON}


def test_unreadable_controllers_fail_closed() -> None:
    snapshot = _stale_snapshot(ROLLOUT, BARE_RS, CRON)
    report = filter_live_targets(snapshot, end=END, live_containers=set(), scaled_to_zero=None)
    assert report.not_running == {ROLLOUT, BARE_RS, CRON} and not report.gone


def _item(namespace, name, replicas=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name), spec=SimpleNamespace(replicas=replicas)
    )


def _list(items):
    from types import SimpleNamespace

    return SimpleNamespace(items=items)


async def _read(monkeypatch, *, rollouts=None, rollout_error=None, cron_error=None):
    from unittest.mock import AsyncMock, MagicMock

    from greenkube.core import k8s_client
    from greenkube.core.optimization.liveness import read_scaled_to_zero

    apps = MagicMock()
    apps.list_deployment_for_all_namespaces = AsyncMock(return_value=_list([_item("shop", "d0", 0)]))
    apps.list_stateful_set_for_all_namespaces = AsyncMock(return_value=_list([]))
    apps.list_replica_set_for_all_namespaces = AsyncMock(
        return_value=_list([_item("shop", "my-rs", 0), _item("shop", "busy-rs", 2)])
    )
    batch = MagicMock()
    batch.list_cron_job_for_all_namespaces = (
        AsyncMock(side_effect=cron_error) if cron_error else AsyncMock(return_value=_list([_item("shop", "report")]))
    )
    custom = MagicMock()
    custom.list_cluster_custom_object = (
        AsyncMock(side_effect=rollout_error) if rollout_error else AsyncMock(return_value={"items": rollouts or []})
    )
    monkeypatch.setattr(k8s_client, "get_apps_v1_api", AsyncMock(return_value=apps))
    monkeypatch.setattr(k8s_client, "get_batch_v1_api", AsyncMock(return_value=batch))
    monkeypatch.setattr(k8s_client, "get_custom_objects_api", AsyncMock(return_value=custom))
    return await read_scaled_to_zero()


@pytest.mark.asyncio
async def test_controller_reader_lists_rollouts_replicasets_and_cronjobs(monkeypatch) -> None:
    rollouts = [
        {"metadata": {"namespace": "shop", "name": "web"}, "spec": {"replicas": 0}},
        {"metadata": {"namespace": "shop", "name": "live"}, "spec": {"replicas": 3}},
    ]
    present = await _read(monkeypatch, rollouts=rollouts)
    assert present == {("shop", "Deployment", "d0"), BARE_RS, ROLLOUT, CRON}


@pytest.mark.asyncio
async def test_controller_reader_without_the_rollout_crd_lists_the_rest(monkeypatch) -> None:
    from kubernetes_asyncio.client.rest import ApiException

    present = await _read(monkeypatch, rollout_error=ApiException(status=404))
    assert present == {("shop", "Deployment", "d0"), BARE_RS, CRON}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["rollout", "cron"])
async def test_controller_reader_fails_closed_on_a_failed_list(monkeypatch, which) -> None:
    from kubernetes_asyncio.client.rest import ApiException

    error = ApiException(status=403)
    present = await _read(
        monkeypatch, rollout_error=error if which == "rollout" else None, cron_error=error if which == "cron" else None
    )
    assert present is None
