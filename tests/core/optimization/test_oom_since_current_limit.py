"""REC-02: OOM kills and restarts count only under the container's current configuration.

After an OOM fix (memory request and limit raised), the kills recorded under the
old limit stay in the lookback for days. They must neither "prove" that the new
limit was passed (driving a further increase on every scan) nor block a
decrease forever. A kill after the change still counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers.rightsizing import memory_proposal
from greenkube.core.reliability_signals import apply_signals, read_prometheus_signals
from greenkube.core.workload_usage import rollup_rows, summarize
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024
T0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
TARGET = ("shop", "Deployment", "api")
POD = "api-7c9d8f6b5d-abcde"


def _summary(memory: UsageStats, **extra: Any) -> WorkloadUsageSummary:
    values: Dict[str, Any] = dict(
        namespace="shop",
        target_kind="Deployment",
        target_name="api",
        container_name="app",
        first_hour=T0 - timedelta(days=14),
        last_hour=T0,
        hours_observed=14 * 24,
        cpu=UsageStats(sample_count=4032, avg=50, p50=50, p90=60, p95=60, p99=70, max=80),
        memory=memory,
        cpu_request=100,
        memory_request=64 * MIB,
        memory_limit=71 * MIB,
    )
    values.update(extra)
    return WorkloadUsageSummary(**values)


def _snapshot(summary: WorkloadUsageSummary) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="test")
    snapshot.summaries[TARGET] = {"app": summary}
    return snapshot


def _prometheus(kill_at: Optional[datetime], kills: int = 1, restart_at: Optional[datetime] = None):
    """A fake Prometheus: kube-state-metrics scraped, one container with ``kills`` OOM kills."""

    def series(value: float) -> List[Dict[str, Any]]:
        metric = {"namespace": "shop", "pod": POD, "container": "app"}
        return [{"metric": metric, "value": [T0.timestamp(), str(value)]}]

    async def query(promql: str, at: datetime) -> Optional[List[Dict[str, Any]]]:
        del at
        if promql.startswith("count(kube_pod_container_status_restarts_total)"):
            return [{"metric": {}, "value": [T0.timestamp(), "3"]}]
        if "timestamp(" in promql and "OOMKilled" in promql:
            return series(kill_at.timestamp()) if kill_at else []
        if "timestamp(" in promql and "container_oom_events_total" in promql:
            return []
        if "timestamp(" in promql:
            last = restart_at or kill_at
            return series(last.timestamp()) if last else []
        if "OOMKilled" in promql:
            return series(kills) if kill_at else []
        if "kube_pod_container_status_restarts_total" in promql:
            return series(kills) if kill_at else []
        return []

    return query


async def _attach(summary: WorkloadUsageSummary, query) -> WorkloadUsageSummary:
    snapshot = _snapshot(summary)
    signals = await read_prometheus_signals(query, snapshot, window_seconds=14 * 86400, at=T0)
    apply_signals(snapshot, signals)
    return snapshot.summaries[TARGET]["app"]


SMALL = UsageStats(sample_count=4032, avg=30 * MIB, p50=30 * MIB, p90=35 * MIB, p95=35 * MIB, p99=36 * MIB,
                   max=37 * MIB)  # fmt: skip


async def test_kills_before_the_last_limit_change_do_not_drive_another_increase() -> None:
    summary = _summary(SMALL, memory_config_since=T0 - timedelta(hours=1))

    attached = await _attach(summary, _prometheus(kill_at=T0 - timedelta(hours=2)))

    assert attached.oom_kill_count == 0
    assert attached.oom_kills_before_change == 1
    assert memory_proposal(Config(), attached) is None


async def test_a_kill_after_the_change_still_drives_an_increase() -> None:
    summary = _summary(SMALL, memory_config_since=T0 - timedelta(hours=1))

    attached = await _attach(summary, _prometheus(kill_at=T0 - timedelta(minutes=30)))

    assert attached.oom_kill_count == 1
    proposal = memory_proposal(Config(), attached)
    assert proposal is not None and proposal.direction == "increase" and proposal.basis == "oom_limit"


async def test_an_old_kill_under_a_previous_limit_no_longer_blocks_a_decrease() -> None:
    idle = UsageStats(sample_count=4032, avg=20 * MIB, p50=20 * MIB, p90=22 * MIB, p95=22 * MIB, p99=24 * MIB,
                      max=25 * MIB)  # fmt: skip
    summary = _summary(idle, memory_config_since=T0 - timedelta(days=7), memory_request=512 * MIB, memory_limit=None)

    attached = await _attach(summary, _prometheus(kill_at=T0 - timedelta(days=10)))

    assert attached.oom_kill_count == 0 and attached.restarts_in_window == 0
    proposal = memory_proposal(Config(), attached)
    assert proposal is not None and proposal.direction == "decrease"


async def test_without_a_readable_kill_time_the_kills_still_count() -> None:
    summary = _summary(SMALL, memory_config_since=T0 - timedelta(hours=1))
    base = _prometheus(kill_at=T0 - timedelta(hours=2))

    async def failing_timestamps(promql: str, at: datetime):
        if "timestamp(" in promql:
            raise RuntimeError("query timeout")
        return await base(promql, at)

    attached = await _attach(summary, failing_timestamps)

    assert attached.oom_kill_count == 1


def _rows(hours: int, change_at_hour: int) -> tuple[list, list]:
    pods, containers = [], []
    start = T0 - timedelta(hours=hours)
    for index in range(hours * 12):
        ts = start + timedelta(minutes=5 * index)
        before = index < change_at_hour * 12
        base = dict(
            namespace="shop", pod_name=POD, timestamp=ts, owner_kind="Deployment", owner_name="api", node="n1",
            sample_count=1, duration_seconds=300, cpu_usage_millicores=50, cpu_usage_max_millicores=None,
            memory_usage_bytes=30 * MIB, memory_usage_max_bytes=None, cpu_request=100,
            memory_request=32 * MIB if before else 64 * MIB, cpu_limit=None,
            memory_limit=32 * MIB if before else 71 * MIB, restart_count=0, total_cost=0.0, co2e_grams=0.0,
            embodied_co2e_grams=0.0, joules=0.0,
        )  # fmt: skip
        pods.append(SimpleNamespace(**base))
        containers.append(SimpleNamespace(container_name="app", **base))
    return pods, containers


def test_the_summary_records_since_when_the_memory_configuration_is_current() -> None:
    pods, containers = _rows(hours=48, change_at_hour=40)

    summary = summarize(rollup_rows(pods, containers, step_seconds=300)).summaries[TARGET]["app"]

    assert summary.memory_config_since == T0 - timedelta(hours=8)


def test_an_unchanged_configuration_has_no_since() -> None:
    pods, containers = _rows(hours=24, change_at_hour=0)

    summary = summarize(rollup_rows(pods, containers, step_seconds=300)).summaries[TARGET]["app"]

    assert summary.memory_config_since is None


def test_the_source_run_says_the_kills_happened_under_a_previous_limit() -> None:
    from greenkube.core.optimization.analyzers.rightsizing_memory import (
        OOM_KILLS_BEFORE_CHANGE,
        stale_oom_diagnostics,
    )

    [diagnostic] = stale_oom_diagnostics([(TARGET, "app", "memory", OOM_KILLS_BEFORE_CHANGE)])

    assert diagnostic["code"] == OOM_KILLS_BEFORE_CHANGE
    assert "previous memory request or limit" in diagnostic["message"]
