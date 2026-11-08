"""Two-way rightsizing rules shared by the CPU and memory analyzers (roadmap E9).

A container's request is compared with the per-replica usage distribution of
its :class:`~greenkube.models.workload_usage.WorkloadUsageSummary`:

* **Increase** (reliability): p99 usage above ``RIGHTSIZING_INCREASE_THRESHOLD``
  (default 0.9) of the request; for memory also any OOM kill or a working set
  above 90% of the memory limit. Increases need no minimum coverage.
* **Decrease** (efficiency): average usage below the resource threshold, with
  at least ``RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS`` (default 7) of
  observed coverage so weekly cycles are seen.

Limits follow ``RECOMMENDATION_LIMITS_POLICY`` (``keep`` by default): limits
are left alone unless the change would make them invalid or unsafe, in which
case the recommendation states the limit it requires.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Literal, Optional

from greenkube.core.optimization.statistics import balanced_rightsizing_target, coverage_days

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.models.workload_usage import WorkloadUsageSummary

MIB = 1024 * 1024

#: A memory working set above this share of the limit is an OOM risk.
MEMORY_LIMIT_PRESSURE = 0.9
#: CPU throttling above this share of CFS periods asks for a CPU limit change.
CPU_THROTTLING_THRESHOLD = 0.05
#: A CPU limit kept under throttling must be at least p99 x this factor.
CPU_LIMIT_P99_FACTOR = 1.5
#: Memory limits and memory increases cover the observed maximum x this factor.
MEMORY_MAX_FACTOR = 1.1

Direction = Literal["increase", "decrease"]


@dataclass
class RightsizingProposal:
    """A request change for one resource of one container."""

    resource: Literal["cpu", "memory"]
    direction: Direction
    current: int
    proposed: int
    #: Value before the configured minimum floor (decreases only).
    raw_proposed: int
    #: Why the change is proposed (``p99_above_request``, ``oom_kills``, ``low_average_usage`` ...).
    triggers: List[str] = field(default_factory=list)
    #: Limit changes the proposal requires or the policy asks for, as plain sentences.
    limit_advice: List[str] = field(default_factory=list)

    @property
    def floored(self) -> bool:
        return self.proposed != self.raw_proposed


def limits_policy(config: "Config") -> str:
    value = str(getattr(config, "RECOMMENDATION_LIMITS_POLICY", "keep") or "keep").strip().lower()
    return value if value in {"keep", "remove_cpu", "memory_equals_request"} else "keep"


def _coverage_ok(config: "Config", summary: "WorkloadUsageSummary") -> bool:
    required = float(getattr(config, "RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS", 7.0) or 0.0)
    if required <= 0:
        return True
    observed = coverage_days(summary)
    return observed is not None and observed >= required


def decrease_blocked_by_coverage(config: "Config", summary: "WorkloadUsageSummary") -> bool:
    """True when the summary would allow a decrease but its coverage is too short."""
    return not _coverage_ok(config, summary)


def cpu_proposal(config: "Config", summary: "WorkloadUsageSummary") -> Optional[RightsizingProposal]:
    """CPU request change for one container, or None when the request fits."""
    stats = summary.cpu
    request = int(summary.cpu_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)
    throttling = summary.throttling_ratio

    threshold = float(getattr(config, "RIGHTSIZING_INCREASE_THRESHOLD", 0.9))
    if stats.p99 > request * threshold:
        proposed = max(int(math.ceil(stats.p99 * config.RIGHTSIZING_HEADROOM)), request + 1)
        proposal = RightsizingProposal("cpu", "increase", request, proposed, proposed, ["p99_above_request"])
        proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling)
        return proposal

    if stats.avg / request >= config.RIGHTSIZING_CPU_THRESHOLD or not _coverage_ok(config, summary):
        return None
    raw = balanced_rightsizing_target(config, stats.avg, stats.max, stats.p95)
    proposed = max(raw, config.RECOMMENDATION_MIN_CPU_MILLICORES)
    if proposed >= request:
        return None
    proposal = RightsizingProposal("cpu", "decrease", request, proposed, raw, ["low_average_usage"])
    proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling)
    return proposal


def _cpu_limit_advice(
    policy: str, summary: "WorkloadUsageSummary", proposed: int, throttling: Optional[float]
) -> List[str]:
    limit = summary.cpu_limit
    if not limit or limit <= 0:
        return []
    stats = summary.cpu
    p99 = stats.p99 if stats is not None else 0.0
    if policy == "remove_cpu":
        return [f"Remove the CPU limit ({limit}m) (limits policy remove_cpu)."]
    floor = max(proposed, int(math.ceil(p99 * CPU_LIMIT_P99_FACTOR)))
    if proposed > limit:
        return [f"The CPU limit ({limit}m) is below the proposed request: remove it or raise it to at least {floor}m."]
    if throttling is not None and throttling > CPU_THROTTLING_THRESHOLD:
        return [
            f"CPU is throttled in {throttling:.0%} of periods: remove the CPU limit ({limit}m) "
            f"or raise it to at least {floor}m."
        ]
    return []


def _round_up_mib(value: float) -> int:
    return int(math.ceil(value / MIB)) * MIB


def memory_proposal(config: "Config", summary: "WorkloadUsageSummary") -> Optional[RightsizingProposal]:
    """Memory request change for one container, or None when the request fits."""
    stats = summary.memory
    request = int(summary.memory_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)
    limit = summary.memory_limit

    triggers: List[str] = []
    threshold = float(getattr(config, "RIGHTSIZING_INCREASE_THRESHOLD", 0.9))
    if stats.p99 > request * threshold:
        triggers.append("p99_above_request")
    if summary.oom_kill_count:
        triggers.append("oom_kills")
    if limit and stats.max > limit * MEMORY_LIMIT_PRESSURE:
        triggers.append("working_set_near_limit")
    if triggers:
        proposed = _round_up_mib(max(stats.p99 * config.RIGHTSIZING_HEADROOM, stats.max * MEMORY_MAX_FACTOR))
        if proposed <= request:
            # The request already covers usage; only the limit is at risk, and a
            # limit-only change cannot be expressed as a request recommendation.
            return None
        proposal = RightsizingProposal("memory", "increase", request, proposed, proposed, triggers)
        proposal.limit_advice = _memory_limit_advice(policy, summary, proposed)
        return proposal

    if stats.avg / request >= config.RIGHTSIZING_MEMORY_THRESHOLD or not _coverage_ok(config, summary):
        return None
    raw = balanced_rightsizing_target(config, stats.avg, stats.max, stats.p95)
    if policy == "memory_equals_request":
        # The limit will equal the request: it must still cover the observed maximum.
        raw = max(raw, _round_up_mib(stats.max * MEMORY_MAX_FACTOR))
    proposed = max(raw, config.RECOMMENDATION_MIN_MEMORY_BYTES)
    if proposed >= request:
        return None
    proposal = RightsizingProposal("memory", "decrease", request, proposed, raw, ["low_average_usage"])
    proposal.limit_advice = _memory_limit_advice(policy, summary, proposed)
    return proposal


def _memory_limit_advice(policy: str, summary: "WorkloadUsageSummary", proposed: int) -> List[str]:
    stats = summary.memory
    observed_max = stats.max if stats is not None else 0.0
    floor = _round_up_mib(max(float(proposed), observed_max * MEMORY_MAX_FACTOR))
    limit = summary.memory_limit
    if policy == "memory_equals_request":
        return [
            f"Set the memory limit equal to the request ({proposed // MIB}MiB) (limits policy memory_equals_request)."
        ]
    if limit and limit < floor:
        return [
            f"Raise the memory limit ({limit // MIB}MiB) to at least {floor // MIB}MiB: it must cover the request "
            "and 110% of the observed maximum working set."
        ]
    return []
