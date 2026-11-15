"""REC-03: a rightsizing proposal is a fixed point of the rules that produced it.

Applying a decrease and scanning again with unchanged usage must propose
nothing: the decrease target never satisfies the increase trigger (no
decrease, increase, decrease cycle), and a memory decrease never goes below
the observed peak plus headroom. An increase settles in one step, two when
its step was capped.
"""

from __future__ import annotations

import os
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from greenkube.core.config import Config
from greenkube.core.optimization.analyzers.rightsizing import cpu_proposal, memory_proposal
from greenkube.core.workload_usage import rollup_rows, summarize
from greenkube.models.workload_usage import UsageStats, WorkloadUsageSummary

MIB = 1024 * 1024


def _config(**overrides: Any) -> Config:
    return Config().model_copy(update=overrides)


def _summary(cpu: UsageStats, memory: UsageStats, cpu_request: int, memory_request: int, **extra: Any):
    last = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return WorkloadUsageSummary(
        namespace="shop",
        target_kind="Deployment",
        target_name="api",
        container_name="app",
        first_hour=last - timedelta(hours=14 * 24 - 1),
        last_hour=last,
        hours_observed=14 * 24,
        cpu=cpu,
        memory=memory,
        cpu_request=cpu_request,
        memory_request=memory_request,
        oom_kill_count=0,
        restarts_in_window=0,
        **extra,
    )


def _random_stats(rng: random.Random, scale: float) -> UsageStats:
    avg = rng.uniform(0.01, 1.0) * scale
    p95 = avg * rng.uniform(1.0, 3.0)
    p99 = p95 * rng.uniform(1.0, 4.0)
    peak = p99 * rng.uniform(1.0, 2.0)
    return UsageStats(sample_count=4032, avg=avg, p50=avg, p90=p95, p95=p95, p99=p99, max=peak)


def _settle(propose, summary: WorkloadUsageSummary, resource: str, policy: str, steps: int) -> list:
    """Applies successive proposals; returns them (the last element is None when it settled)."""
    trail: list = []
    for _ in range(steps):
        proposal = propose(summary)
        trail.append(proposal)
        if proposal is None:
            return trail
        if resource == "cpu":
            summary = replace(summary, cpu_request=proposal.proposed)
        else:
            limit = summary.memory_limit
            for advice in proposal.limit_advice:
                if advice.action == "equal_to_request":
                    limit = proposal.proposed
                elif advice.action == "raise":
                    limit = advice.minimum
            summary = replace(summary, memory_request=proposal.proposed, memory_limit=limit)
    return trail


@pytest.mark.parametrize("policy", ["keep", "memory_equals_request"])
def test_any_proposal_settles_with_unchanged_usage(policy: str) -> None:
    config = _config(RECOMMENDATION_LIMITS_POLICY=policy)
    rng = random.Random(20261115)
    for _ in range(3000):
        cpu = _random_stats(rng, 2000)
        memory = _random_stats(rng, 4096 * MIB)
        summary = _summary(
            cpu,
            memory,
            cpu_request=rng.randint(10, 8000),
            memory_request=rng.randint(16, 16384) * MIB,
        )
        for resource, propose in (
            ("cpu", lambda s: cpu_proposal(config, s)),
            ("memory", lambda s: memory_proposal(config, s)),
        ):
            first = propose(summary)
            if first is None:
                continue
            # One step, or one more after an uncapped increase; a capped increase repeats its step.
            steps = 2 if first.direction == "decrease" or first.proposed == first.raw_proposed else 12
            trail = _settle(propose, summary, resource, policy, steps)
            directions = [p.direction for p in trail if p is not None]
            assert len(set(directions)) <= 1, (resource, policy, directions)
            assert trail[-1] is None, (resource, policy, [(p.direction, p.current, p.proposed) for p in trail if p])
            if resource == "memory" and first.direction == "decrease":
                assert first.proposed >= memory.max * 1.1


def test_memory_decrease_never_goes_below_the_observed_peak_with_headroom() -> None:
    memory = UsageStats(sample_count=4032, avg=60 * MIB, p50=60 * MIB, p90=80 * MIB, p95=90 * MIB, p99=300 * MIB,
                        max=400 * MIB)  # fmt: skip
    cpu = UsageStats(sample_count=4032, avg=100, p50=100, p90=100, p95=100, p99=100, max=100)
    proposal = memory_proposal(_config(), _summary(cpu, memory, cpu_request=200, memory_request=2048 * MIB))
    assert proposal is not None and proposal.direction == "decrease"
    assert proposal.proposed >= 440 * MIB


def _build(cpu_req: int, mem_req: int, profile, days: int = 14, step: int = 300):
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    pods, containers = [], []
    for index in range(days * 24 * 3600 // step):
        ts = t0 + timedelta(seconds=index * step)
        cpu, mem = profile(ts.hour, index)
        base = dict(
            namespace="shop",
            pod_name="api-7c9d8f6b5d-abcde",
            timestamp=ts,
            owner_kind="Deployment",
            owner_name="api",
            node="n1",
            sample_count=1,
            duration_seconds=step,
            cpu_usage_millicores=cpu,
            cpu_usage_max_millicores=None,
            memory_usage_bytes=mem,
            memory_usage_max_bytes=None,
            cpu_request=cpu_req,
            memory_request=mem_req,
            cpu_limit=None,
            memory_limit=None,
            restart_count=0,
            total_cost=0.0,
            co2e_grams=0.0,
            embodied_co2e_grams=0.0,
            joules=0.0,
        )
        pods.append(SimpleNamespace(**base))
        containers.append(SimpleNamespace(container_name="app", **base))
    snapshot = summarize(rollup_rows(pods, containers, step_seconds=step, warmup_seconds=600))
    target = ("shop", "Deployment", "api")
    summary = snapshot.summaries[target]["app"]
    summary.oom_kill_count = 0
    summary.restarts_in_window = 0
    return summary, snapshot.summaries[target][""]


PROFILES = {
    "daily-1h-peak": (lambda h, i: (400, 400 * MIB) if h == 3 else (100, 100 * MIB), 1000, 1024 * MIB),
    "business-hours": (lambda h, i: (300, 600 * MIB) if 9 <= h < 17 else (60, 200 * MIB), 2000, 4096 * MIB),
    "5-min-spike": (lambda h, i: (500, 800 * MIB) if (h == 12 and i % 12 == 0) else (50, 150 * MIB), 1000, 2048 * MIB),
}


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_flap_profiles_are_stable_after_one_change(name: str) -> None:
    profile, cpu_req, mem_req = PROFILES[name]
    config = Config(_env_file=None, DB_TYPE="sqlite", DB_PATH=os.devnull)  # pyrefly: ignore[unexpected-keyword]
    proposals: list[tuple[str, Optional[int], Optional[int]]] = []
    for scan in range(3):
        summary, pod = _build(cpu_req, mem_req, profile)
        cpu, memory = cpu_proposal(config, summary, pod), memory_proposal(config, summary, pod)
        if cpu is None and memory is None:
            assert scan <= 1, proposals
            return
        proposals.append((f"scan {scan}", cpu.proposed if cpu else None, memory.proposed if memory else None))
        cpu_req = cpu.proposed if cpu else cpu_req
        mem_req = memory.proposed if memory else mem_req
    pytest.fail(f"never stable: {proposals}")
