"""OOM-killed containers get a bounded memory increase, never a decrease (wave-4 e2e).

A memory spike shorter than the 30-second scrape leaves the observed maximum
far below the limit the kill proves was reached: the increase must be sized
from that limit (bounded by ``RIGHTSIZING_MAX_INCREASE_FACTOR``), and a
container that was OOM-killed or restarted in the window is never offered a
memory decrease.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import MemoryRightsizingAnalyzer
from greenkube.core.optimization.analyzers.rightsizing import cpu_proposal, memory_proposal
from greenkube.core.optimization.context import OptimizationContext
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024


def _stats(avg: float, peak: float) -> UsageStats:
    return UsageStats(sample_count=4032, avg=avg, p50=avg, p90=avg, p95=avg, p99=avg, max=peak)


def _summary(**overrides: Any) -> WorkloadUsageSummary:
    last = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    values: dict[str, Any] = {
        "namespace": "demo",
        "target_kind": "Deployment",
        "target_name": "oom-demo",
        "container_name": "app",
        "first_hour": last - timedelta(days=14),
        "last_hour": last,
        "hours_observed": 14 * 24,
        # The 30 s samples only saw the idle working set: the spike to the limit was missed.
        "memory": _stats(avg=6 * MIB, peak=8 * MIB),
        "memory_request": 64 * MIB,
        "memory_limit": 64 * MIB,
        "cpu_request": 100,
    }
    values.update(overrides)
    return WorkloadUsageSummary(**values)


def _snapshot(summary: WorkloadUsageSummary) -> UsageSnapshot:
    snapshot = UsageSnapshot(source="rollup")
    snapshot.summaries.setdefault(summary.target, {})[summary.container_name] = summary
    return snapshot


def test_an_oom_kill_with_a_missed_spike_is_sized_from_the_limit_it_proves() -> None:
    """Regression: the observed maximum (8Mi) is below the request, so no increase was proposed."""
    proposal = memory_proposal(Config(), _summary(oom_kill_count=2))

    assert proposal is not None
    assert proposal.direction == "increase"
    assert "oom_kills" in proposal.triggers
    assert proposal.basis == "oom_limit"
    assert proposal.proposed == math.ceil(64 * 1.1) * MIB
    # The request cannot exceed the limit the kill happened at: the limit is raised with it.
    [advice] = proposal.limit_advice
    assert advice.resource == "memory" and advice.action == "raise"
    assert advice.minimum is not None and advice.minimum >= proposal.proposed


def test_the_limit_derived_increase_is_bounded_by_the_max_increase_factor() -> None:
    summary = _summary(oom_kill_count=1, memory_request=100 * MIB, memory_limit=1024 * MIB)

    default = memory_proposal(Config(), summary)
    wide = memory_proposal(Config().model_copy(update={"RIGHTSIZING_MAX_INCREASE_FACTOR": 3.0}), summary)

    assert default is not None and default.proposed == 200 * MIB
    assert wide is not None and wide.proposed == 300 * MIB


def test_an_oom_kill_without_a_limit_is_sized_from_the_request() -> None:
    proposal = memory_proposal(Config(), _summary(oom_kill_count=1, memory_limit=None))

    assert proposal is not None and proposal.direction == "increase"
    assert proposal.proposed == math.ceil(64 * 1.1) * MIB


def test_an_observed_peak_above_the_limit_bound_still_sizes_the_increase() -> None:
    summary = _summary(
        oom_kill_count=1, memory_request=100 * MIB, memory_limit=None, memory=_stats(80 * MIB, 500 * MIB)
    )

    proposal = memory_proposal(Config(), summary)

    assert proposal is not None and proposal.basis == "observed_max"
    assert proposal.proposed == 550 * MIB


def test_an_oom_killed_container_is_never_offered_a_decrease() -> None:
    oversized = _summary(oom_kill_count=1, memory_request=1024 * MIB, memory_limit=None)

    proposal = memory_proposal(Config(), oversized)

    assert proposal is None or proposal.direction == "increase"


def test_restarts_in_the_window_block_a_memory_decrease() -> None:
    """Regression: a decrease was proposed before the first OOM kill was recorded."""
    oversized = {"memory_request": 1024 * MIB, "memory_limit": None, "oom_kill_count": 0}

    healthy = memory_proposal(Config(), _summary(**oversized, restarts_in_window=0))
    restarted = memory_proposal(Config(), _summary(**oversized, restarts_in_window=3))

    assert healthy is not None and healthy.direction == "decrease"
    assert restarted is None


def test_memory_decreases_are_whole_mebibytes() -> None:
    summary = _summary(memory_request=1024 * MIB, memory_limit=None, memory=_stats(100 * MIB + 12345, 140 * MIB + 777))

    proposal = memory_proposal(Config(), summary)

    assert proposal is not None and proposal.direction == "decrease"
    assert proposal.proposed % MIB == 0
    assert proposal.proposed >= proposal.raw_proposed - MIB


def test_a_crashlooping_container_is_never_offered_a_cpu_decrease() -> None:
    """e2e: the CPU decrease shared the OOM fix's action, so the fix was not increase-only and stayed blocked."""
    idle = {"cpu": _stats(avg=5, peak=8), "cpu_request": 300}
    healthy = cpu_proposal(Config(), _summary(**idle))

    assert healthy is not None and healthy.direction == "decrease"
    assert cpu_proposal(Config(), _summary(**idle, oom_kill_count=2)) is None
    assert cpu_proposal(Config(), _summary(**idle, restarts_in_window=3)) is None


def test_the_analyzer_explains_the_limit_based_sizing() -> None:
    context = OptimizationContext(config=Config(), usage=_snapshot(_summary(oom_kill_count=2)))

    (rec,) = MemoryRightsizingAnalyzer(context.config).analyze(context)

    assert rec.recommended_memory_request_bytes == 71 * MIB
    assert rec.current_memory_request_bytes == 64 * MIB
    assert "OOM-killed" in rec.reason
    assert "memory limit (64MiB)" in rec.reason
