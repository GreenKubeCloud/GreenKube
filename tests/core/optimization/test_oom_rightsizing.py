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
        # Known-clean reliability history unless a test says otherwise (REC-04: unknown withholds decreases).
        "oom_kill_count": 0,
        "restarts_in_window": 0,
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
    # A short spike (max 8Mi far below the 64Mi limit): limit x 1.5, never the 1.1x creep.
    assert proposal.proposed == 96 * MIB
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
    assert proposal.proposed == 96 * MIB


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

    assert rec.recommended_memory_request_bytes == 96 * MIB
    assert rec.current_memory_request_bytes == 64 * MIB
    assert "OOM-killed" in rec.reason
    assert "memory limit (64MiB)" in rec.reason


def _pinned_at_limit(size_mib: int = 32, **overrides: Any) -> WorkloadUsageSummary:
    """Live e2e: request = limit, the working set sits at the limit and the kernel kills it there."""
    values: dict[str, Any] = {
        "oom_kill_count": 3,
        "restarts_in_window": 3,
        "memory_request": size_mib * MIB,
        "memory_limit": size_mib * MIB,
        "memory": UsageStats(
            sample_count=576,
            avg=size_mib * 0.8 * MIB,
            p50=size_mib * 0.8 * MIB,
            p90=size_mib * 0.95 * MIB,
            p95=size_mib * 0.97 * MIB,
            p99=size_mib * 0.98 * MIB,
            max=size_mib * 0.99 * MIB,
        ),
    }
    values.update(overrides)
    return _summary(**values)


def test_an_oom_kill_at_the_limit_takes_one_decisive_step() -> None:
    """Regression (live e2e): 32Mi -> 36Mi -> 40Mi ..., one pull request and measurement window per step."""
    proposal = memory_proposal(Config(), _pinned_at_limit())

    assert proposal is not None and proposal.basis == "oom_limit"
    assert proposal.proposed == 64 * MIB
    [advice] = proposal.limit_advice
    assert advice.reason == "oom_headroom"
    assert advice.minimum is not None and advice.minimum > proposal.proposed
    assert advice.minimum == math.ceil(64 * 1.1) * MIB


def test_the_decisive_oom_step_follows_the_configured_cap() -> None:
    summary = _pinned_at_limit()

    wide = memory_proposal(Config().model_copy(update={"RIGHTSIZING_MAX_INCREASE_FACTOR": 3.0}), summary)
    narrow = memory_proposal(Config().model_copy(update={"RIGHTSIZING_MAX_INCREASE_FACTOR": 1.25}), summary)

    assert wide is not None and wide.proposed == 96 * MIB
    assert narrow is not None and narrow.proposed == 40 * MIB


def test_a_short_spike_oom_step_never_exceeds_the_configured_cap() -> None:
    narrow = Config().model_copy(update={"RIGHTSIZING_MAX_INCREASE_FACTOR": 1.25})

    proposal = memory_proposal(narrow, _summary(oom_kill_count=2))

    assert proposal is not None and proposal.proposed == 80 * MIB


def test_the_oom_step_doubles_the_limit_when_the_request_is_below_it() -> None:
    summary = _pinned_at_limit(memory_request=16 * MIB)

    proposal = memory_proposal(Config(), summary)

    # The request is sized from usage (p99 x 1.2 = 38Mi, above 2x the request: OOM steps are uncapped)...
    assert proposal is not None and proposal.proposed == 38 * MIB
    [advice] = proposal.limit_advice
    # ...and the limit the kills happen at doubles (2x the limit, the configured bound), above the request.
    assert advice.minimum == 64 * MIB > proposal.proposed


def test_a_non_oom_increase_keeps_its_p99_sizing() -> None:
    busy = _summary(memory_request=100 * MIB, memory_limit=None, memory=_stats(95 * MIB, 99 * MIB))

    proposal = memory_proposal(Config(), busy)

    assert proposal is not None and proposal.basis == "p99"
    assert proposal.proposed == math.ceil(95 * 1.2) * MIB


def test_limit_pressure_without_oom_keeps_its_observed_max_sizing() -> None:
    pressured = _summary(
        memory_request=30 * MIB, memory_limit=32 * MIB, memory=_stats(20 * MIB, 31 * MIB), oom_kill_count=0
    )

    proposal = memory_proposal(Config(), pressured)

    assert proposal is not None and proposal.basis == "observed_max"
    assert proposal.proposed == math.ceil(31 * 1.1) * MIB


def test_the_shared_planner_scenario_is_the_analyzer_step() -> None:
    """The planner tests render 24Mi -> 48Mi: that is the step the analyzer proposes for the scenario."""
    from tests.automation.actions.oom_limit_scenario import (  # pyrefly: ignore[missing-import]
        OOM_PROPOSED,
        oom_summary,
    )

    proposal = memory_proposal(Config(), oom_summary())

    assert proposal is not None and proposal.proposed == OOM_PROPOSED


def test_the_analyzer_explains_the_decisive_step() -> None:
    spike = OptimizationContext(config=Config(), usage=_snapshot(_summary(oom_kill_count=2)))
    pinned = OptimizationContext(config=Config(), usage=_snapshot(_pinned_at_limit()))

    (spike_rec,) = MemoryRightsizingAnalyzer(spike.config).analyze(spike)
    (pinned_rec,) = MemoryRightsizingAnalyzer(pinned.config).analyze(pinned)

    assert "memory limit (64MiB) the OOM kills prove was passed x 1.5" in spike_rec.reason
    assert "short spike" in spike_rec.reason
    assert pinned_rec.recommended_memory_request_bytes == 64 * MIB
    assert "memory limit (32MiB) the OOM kills prove was passed x 2" in pinned_rec.reason
    assert "one decisive step" in pinned_rec.reason
