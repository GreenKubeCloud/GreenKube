"""Rightsizing fails closed when reliability signals are unknown (REC-04, WP-06.1).

``oom_kill_count`` / ``restarts_in_window`` are None when kube-state-metrics is
not scraped or Prometheus fails. None must never behave like 0: a decrease
needs explicit zeros, or a rollup restart history that shows no restart
(``restart_increase == 0``). Otherwise the decrease is withheld and the
reason ``reliability_signals_unknown`` is reported.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer, MemoryRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import (
    RELIABILITY_SIGNALS_UNKNOWN,
    cpu_proposal,
    memory_proposal,
    withheld_decrease,
)
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.eligibility import CODES
from greenkube.core.optimization.risks import WorkloadRiskProfile, apply_profile
from greenkube.core.reliability_signals import attach_reliability_signals
from greenkube.core.workload_usage import rollup_rows, summarize
from greenkube.models.metrics import Recommendation, RecommendationType, RiskLevel
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary
from greenkube.signals.adapters import analyzer_source_class
from greenkube.signals.sdk import CollectContext, SourceDeps, SourceSettings

MIB = 1024 * 1024
END = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
TARGET = ("shop", "Deployment", "api")


def _summary(**overrides: Any) -> WorkloadUsageSummary:
    values: Dict[str, Any] = {
        "namespace": TARGET[0],
        "target_kind": TARGET[1],
        "target_name": TARGET[2],
        "container_name": "app",
        "first_hour": END - timedelta(days=14),
        "last_hour": END,
        "hours_observed": 14 * 24,
        "cpu": UsageStats(sample_count=4032, avg=20, p50=20, p90=22, p95=24, p99=25, max=30),
        "memory": UsageStats(
            sample_count=4032, avg=100 * MIB, p50=100 * MIB, p90=100 * MIB, p95=100 * MIB, p99=100 * MIB, max=110 * MIB
        ),
        "cpu_request": 1000,
        "memory_request": 1024 * MIB,
    }
    values.update(overrides)
    return WorkloadUsageSummary(**values)


def _snapshot(summary: WorkloadUsageSummary) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="rollup")
    snapshot.summaries.setdefault(summary.target, {})[summary.container_name] = summary
    return snapshot


def test_explicit_zero_signals_allow_a_decrease() -> None:
    summary = _summary(oom_kill_count=0, restarts_in_window=0)
    cpu, memory = cpu_proposal(Config(), summary), memory_proposal(Config(), summary)
    assert cpu is not None and cpu.direction == "decrease"
    assert memory is not None and memory.direction == "decrease"


def test_unknown_signals_with_a_clean_rollup_restart_history_allow_a_decrease() -> None:
    summary = _summary(oom_kill_count=None, restarts_in_window=None, restart_increase=0)
    cpu, memory = cpu_proposal(Config(), summary), memory_proposal(Config(), summary)
    assert cpu is not None and cpu.direction == "decrease"
    assert memory is not None and memory.direction == "decrease"
    assert withheld_decrease(Config(), summary, None, "memory") is None


@pytest.mark.parametrize("restart_increase", [3, None])
def test_unknown_signals_without_a_clean_rollup_history_withhold_the_decrease(restart_increase) -> None:
    summary = _summary(oom_kill_count=None, restarts_in_window=None, restart_increase=restart_increase)
    assert cpu_proposal(Config(), summary) is None
    assert memory_proposal(Config(), summary) is None
    assert withheld_decrease(Config(), summary, None, "cpu") == RELIABILITY_SIGNALS_UNKNOWN
    assert withheld_decrease(Config(), summary, None, "memory") == RELIABILITY_SIGNALS_UNKNOWN


def test_one_unknown_signal_is_enough_to_need_the_rollup_fallback() -> None:
    summary = _summary(oom_kill_count=0, restarts_in_window=None, restart_increase=None)
    assert memory_proposal(Config(), summary) is None
    assert withheld_decrease(Config(), summary, None, "memory") == RELIABILITY_SIGNALS_UNKNOWN


def test_known_restarts_block_the_decrease_without_an_unknown_skip() -> None:
    summary = _summary(oom_kill_count=0, restarts_in_window=4)
    assert memory_proposal(Config(), summary) is None
    assert withheld_decrease(Config(), summary, None, "memory") is None


def test_no_skip_reason_when_usage_does_not_call_for_a_decrease() -> None:
    busy = _summary(cpu=UsageStats(sample_count=4032, avg=700, p50=700, p90=750, p95=780, p99=800, max=850))
    assert withheld_decrease(Config(), busy, None, "cpu") is None


def _rows(samples: List[tuple]) -> WorkloadUsageSummary:
    """sim_gates.py: rollup of a container whose restart count grows by one every 110 steps."""
    pods, containers = [], []
    for ts, cpu, mem, restarts in samples:
        base = dict(
            namespace="shop",
            pod_name="api-7c9d8f6b5d-abcde",
            timestamp=ts,
            owner_kind="Deployment",
            owner_name="api",
            node="n1",
            sample_count=1,
            duration_seconds=300,
            cpu_usage_millicores=cpu,
            cpu_usage_max_millicores=None,
            memory_usage_bytes=mem,
            memory_usage_max_bytes=None,
            cpu_request=1000,
            memory_request=1024 * MIB,
            cpu_limit=None,
            memory_limit=1024 * MIB,
            restart_count=restarts,
            total_cost=0.0,
            co2e_grams=0.0,
            embodied_co2e_grams=0.0,
            joules=0.0,
        )
        pods.append(NS(**base))
        containers.append(NS(container_name="app", **base))
    snapshot = summarize(rollup_rows(pods, containers, step_seconds=300, warmup_seconds=600))
    return snapshot.summaries[TARGET]["app"]


def test_sim_gates_case_1_crash_looping_container_with_unknown_signals_gets_no_decrease() -> None:
    t0 = END - timedelta(days=14)
    full = [(t0 + timedelta(minutes=5 * i), 20, 100 * MIB, i // 110) for i in range(14 * 288)]
    summary = _rows(full)
    assert summary.oom_kill_count is None and summary.restarts_in_window is None
    assert summary.restart_increase is not None and summary.restart_increase >= 30

    assert cpu_proposal(Config(), summary) is None
    assert memory_proposal(Config(), summary) is None
    assert withheld_decrease(Config(), summary, None, "memory") == RELIABILITY_SIGNALS_UNKNOWN


def test_summarize_counts_restart_count_resets_as_restarts() -> None:
    t0 = END - timedelta(hours=4)
    # Hour 0: 2, hour 1: 5 (+3), hour 2: new pod at 1 (reset: +1), hour 3: 1 (+0).
    counts = [2, 5, 1, 1]
    samples = [(t0 + timedelta(hours=h, minutes=5 * s), 20, 100 * MIB, counts[h]) for h in range(4) for s in range(12)]
    assert _rows(samples).restart_increase == 4


def test_analyzers_record_the_skip_and_the_source_reports_it() -> None:
    summary = _summary(oom_kill_count=None, restarts_in_window=None, restart_increase=None)
    context = OptimizationContext(config=Config(), usage=_snapshot(summary), analysis_window_seconds=14 * 86400)

    for analyzer_cls, resource in ((CpuRightsizingAnalyzer, "cpu"), (MemoryRightsizingAnalyzer, "memory")):
        analyzer = analyzer_cls(context.config)
        assert analyzer.analyze(context) == []
        assert (TARGET, "app", resource, RELIABILITY_SIGNALS_UNKNOWN) in analyzer.skipped

    source_cls = analyzer_source_class(MemoryRightsizingAnalyzer)
    source = source_cls(SourceSettings(), SourceDeps(config=context.config, cluster_uid="c"))
    ctx = CollectContext(cluster_uid="c", namespace=None, window=None, deadline=END, optimization=context)
    import asyncio

    batch = asyncio.run(source.collect(ctx))
    assert batch.recommendations == []
    # Previous advice is kept: the scan's rightsizing result is not authoritative.
    assert batch.complete is False
    [diagnostic] = [d for d in batch.diagnostics if d.code == RELIABILITY_SIGNALS_UNKNOWN]
    assert diagnostic.level == "warning" and diagnostic.count == 1
    assert "restart/OOM history could not be read" in diagnostic.message


class _Query:
    """Fake Prometheus: the OOM query fails (HTTP 503 -> None), the others answer."""

    def __init__(self, answers: Dict[str, Optional[List[Dict[str, Any]]]], raises: tuple = ()):
        self.answers = answers
        self.raises = raises

    async def __call__(self, promql: str, at: datetime):
        for fragment in self.raises:
            if fragment in promql:
                raise RuntimeError("timeout")
        for fragment, answer in self.answers.items():
            if fragment in promql:
                return answer
        return []


def _series(value: float, **labels: str) -> Dict[str, Any]:
    return {"metric": labels, "value": [END.timestamp(), str(value)]}


async def _no_pods() -> List[Any]:
    return []


@pytest.mark.asyncio
async def test_a_failed_oom_query_leaves_oom_unknown_not_zero() -> None:
    snapshot = _snapshot(_summary())
    query = _Query(
        {
            "count(kube_pod_container_status_restarts_total)": [_series(3)],
            "kube_pod_container_status_last_terminated_reason": None,
            "container_oom_events_total": None,
        }
    )
    await attach_reliability_signals(
        snapshot, Config(), start=END - timedelta(days=14), end=END, query=query, list_pods=_no_pods
    )
    summary = snapshot.summaries[TARGET]["app"]
    assert summary.oom_kill_count is None
    assert summary.restarts_in_window == 0  # the restart query answered
    assert snapshot.reliability_signal_status["oom_kills"] == "failed"
    assert snapshot.reliability_signal_status["restarts"] == "ok"


@pytest.mark.asyncio
async def test_one_raising_query_does_not_drop_the_other_signals() -> None:
    snapshot = _snapshot(_summary(cpu_limit=1000))
    query = _Query(
        {
            "count(kube_pod_container_status_restarts_total)": [_series(3)],
            "container_cpu_cfs_throttled_periods_total": [
                _series(10, namespace="shop", pod="api-7d9f8b6c5d-abcde", container="app")
            ],
            "container_cpu_cfs_periods_total": [
                _series(100, namespace="shop", pod="api-7d9f8b6c5d-abcde", container="app")
            ],
        },
        raises=("round(increase(kube_pod_container_status_restarts_total{",),
    )
    await attach_reliability_signals(
        snapshot, Config(), start=END - timedelta(days=14), end=END, query=query, list_pods=_no_pods
    )
    summary = snapshot.summaries[TARGET]["app"]
    assert summary.restarts_in_window is None
    assert summary.oom_kill_count == 0
    assert summary.throttling_ratio == pytest.approx(0.1)
    assert snapshot.reliability_signal_status["restarts"] == "failed"


@pytest.mark.asyncio
async def test_without_prometheus_the_signals_are_reported_unavailable() -> None:
    snapshot = _snapshot(_summary())
    config = Config().model_copy(update={"PROMETHEUS_URL": ""})
    await attach_reliability_signals(snapshot, config, start=END - timedelta(days=14), end=END, list_pods=_no_pods)
    assert snapshot.reliability_signal_status["oom_kills"] == "unavailable"
    assert snapshot.reliability_signal_status["restarts"] == "unavailable"


def _memory_decrease() -> Recommendation:
    return Recommendation(
        pod_name="api",
        namespace="shop",
        container_name="app",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="d",
        current_memory_request_bytes=1024 * MIB,
        recommended_memory_request_bytes=512 * MIB,
    )


def test_unknown_oom_history_makes_a_memory_decrease_at_least_medium_risk() -> None:
    level, factors = apply_profile(
        _memory_decrease(), RiskLevel.LOW, [], WorkloadRiskProfile(oom_events=None), requires_restart=False
    )
    assert level in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert "oom_history_unknown" in factors

    level, factors = apply_profile(
        _memory_decrease(), RiskLevel.LOW, [], WorkloadRiskProfile(oom_events=0), requires_restart=False
    )
    assert "oom_history_unknown" not in factors


def test_container_summaries_fall_back_on_the_pod_level_restart_history() -> None:
    """Restart counts are stored per pod: a container summary has none of its own."""
    container = _summary(oom_kill_count=None, restarts_in_window=None, restart_increase=None)
    clean_pod = _summary(container_name="", restart_increase=0)
    restarted_pod = _summary(container_name="", restart_increase=2)

    assert memory_proposal(Config(), container, clean_pod) is not None
    assert cpu_proposal(Config(), container, clean_pod) is not None
    assert memory_proposal(Config(), container, restarted_pod) is None
    assert withheld_decrease(Config(), container, restarted_pod, "memory") == RELIABILITY_SIGNALS_UNKNOWN


def test_risk_profile_reads_no_oom_from_a_clean_rollup_restart_history() -> None:
    from greenkube.core.optimization.enrich import build_risk_profile

    container = _summary(oom_kill_count=None, restarts_in_window=None, restart_increase=None)
    snapshot = _snapshot(container)
    snapshot.summaries[TARGET][""] = _summary(container_name="", restart_increase=0)
    context = OptimizationContext(config=Config(), usage=snapshot)
    rec = _memory_decrease().model_copy(update={"owner_kind": "Deployment", "owner_name": "api"})
    assert build_risk_profile(rec, context).oom_events == 0
    snapshot.summaries[TARGET][""] = _summary(container_name="", restart_increase=None)
    assert build_risk_profile(rec, context).oom_events is None


def test_eligibility_blocks_automation_on_unknown_oom_history() -> None:
    from greenkube.core.optimization.eligibility import EligibilityContext, assess_eligibility

    assert "oom_history_unknown" in CODES
    record = NS(
        id=1,
        type="RIGHTSIZING_MEMORY",
        status="active",
        risk_factors=["oom_history_unknown"],
        value=None,
        consensus=None,
    )
    result = assess_eligibility(record, EligibilityContext(now=END))
    assert "oom_history_unknown" in {entry["code"] for entry in result["blockers"]}
    assert result["can_open_pr"] is False
