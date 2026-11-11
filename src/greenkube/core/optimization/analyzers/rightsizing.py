"""Two-way rightsizing rules shared by the CPU and memory analyzers (roadmap E9).

A container's request is compared with the per-replica usage distribution of
its :class:`~greenkube.models.workload_usage.WorkloadUsageSummary` (one value
per replica and step, never a sum across replicas or pods):

* **Increase** (reliability): p99 usage above ``RIGHTSIZING_INCREASE_THRESHOLD``
  (default 0.9) of the request; for memory also any OOM kill or a working set
  above 90% of the memory limit. Increases need no minimum coverage.
* **Decrease** (efficiency): average usage below the resource threshold, with
  at least ``RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS`` (default 7) of
  observed coverage so weekly cycles are seen.

How an increase is sized (USER BUG 2, docs/recommendation.md):

* the target is the per-replica p99 of per-step usage x ``RIGHTSIZING_HEADROOM``;
  usage percentiles exclude the warm-up after a container (re)start;
* the observed maximum (x 1.1) only raises a memory target when OOM kills or
  limit pressure show that peaks matter; a single startup spike does not;
* one step is capped at ``RIGHTSIZING_MAX_INCREASE_FACTOR`` (default 2) x the
  current request unless OOM kills back it; the next scan re-evaluates;
* a usage summary that only exists at pod level (no container series) never
  produces an increase: it is skipped with the reason ``pod_level_usage_only``.

Limits follow ``RECOMMENDATION_LIMITS_POLICY`` (``keep`` by default): limits
are left alone unless the change would make them invalid or unsafe, in which
case the recommendation states the limit it requires as a structured
:class:`LimitAdvice`.
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
#: Memory limits, and memory increases backed by OOM kills or limit pressure, cover the observed maximum x this.
MEMORY_MAX_FACTOR = 1.1
#: Default cap of one increase step, as a multiple of the current request.
DEFAULT_MAX_INCREASE_FACTOR = 2.0

#: Skip reason of a target whose usage is only known for the whole pod.
POD_LEVEL_USAGE_ONLY = "pod_level_usage_only"

Direction = Literal["increase", "decrease"]
Resource = Literal["cpu", "memory"]


@dataclass(frozen=True)
class LimitAdvice:
    """A limit change a rightsizing proposal requires or the limits policy asks for."""

    resource: Resource
    #: ``remove``, ``raise`` (to at least ``minimum``) or ``equal_to_request``.
    action: Literal["remove", "raise", "equal_to_request"]
    #: Why: ``below_request``, ``throttling``, ``below_floor`` or ``policy``.
    reason: str
    #: Current limit (millicores or bytes), None when unset.
    current: Optional[int]
    #: Smallest acceptable limit (millicores or bytes), None for a removal.
    minimum: Optional[int]
    #: Human-readable sentence shown in the recommendation.
    text: str

    def __str__(self) -> str:
        return self.text


@dataclass
class RightsizingProposal:
    """A request change for one resource of one container."""

    resource: Resource
    direction: Direction
    current: int
    proposed: int
    #: Value before the configured minimum floor (decreases) or the step cap (increases).
    raw_proposed: int
    #: Why the change is proposed (``p99_above_request``, ``oom_kills``, ``low_average_usage`` ...).
    triggers: List[str] = field(default_factory=list)
    #: Limit changes the proposal requires or the policy asks for.
    limit_advice: List[LimitAdvice] = field(default_factory=list)
    #: Statistic the target is sized from: ``p99`` or ``observed_max`` (increases), ``balanced`` (decreases).
    basis: str = "p99"
    #: Cap of one increase step, as a multiple of the current request (None = not capped by rule).
    cap_factor: Optional[float] = None

    @property
    def floored(self) -> bool:
        return self.direction == "decrease" and self.proposed != self.raw_proposed

    @property
    def capped(self) -> bool:
        """True when the increase was limited to ``cap_factor`` x the current request."""
        return self.direction == "increase" and self.proposed < self.raw_proposed

    @property
    def limit_text(self) -> str:
        return "".join(f" {advice.text}" for advice in self.limit_advice)


def limits_policy(config: "Config") -> str:
    value = str(getattr(config, "RECOMMENDATION_LIMITS_POLICY", "keep") or "keep").strip().lower()
    return value if value in {"keep", "remove_cpu", "memory_equals_request"} else "keep"


def max_increase_factor(config: "Config") -> float:
    try:
        value = float(getattr(config, "RIGHTSIZING_MAX_INCREASE_FACTOR", DEFAULT_MAX_INCREASE_FACTOR))
    except (TypeError, ValueError):
        return DEFAULT_MAX_INCREASE_FACTOR
    return value if math.isfinite(value) and value > 1 else DEFAULT_MAX_INCREASE_FACTOR


def _coverage_ok(config: "Config", summary: "WorkloadUsageSummary") -> bool:
    required = float(getattr(config, "RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS", 7) or 0.0)
    if required <= 0:
        return True
    observed = coverage_days(summary)
    return observed is not None and observed >= required


def decrease_blocked_by_coverage(config: "Config", summary: "WorkloadUsageSummary") -> bool:
    """True when the summary would allow a decrease but its coverage is too short."""
    return not _coverage_ok(config, summary)


def increase_skip_reason(summary: "WorkloadUsageSummary") -> Optional[str]:
    """Why an increase must not be proposed from this summary, or None.

    A pod-level summary (``container_name == ""``) aggregates every container of
    the pod: an increase computed from it would be attributed to no container
    in particular, so GreenKube does not invent one (fail closed and explain).
    """
    return POD_LEVEL_USAGE_ONLY if not summary.container_name else None


def _threshold(config: "Config") -> float:
    return float(getattr(config, "RIGHTSIZING_INCREASE_THRESHOLD", 0.9))


def _memory_triggers(config: "Config", summary: "WorkloadUsageSummary") -> List[str]:
    stats = summary.memory
    request = int(summary.memory_request or 0)
    if stats is None or request <= 0:
        return []
    triggers: List[str] = []
    if stats.p99 > request * _threshold(config):
        triggers.append("p99_above_request")
    if summary.oom_kill_count:
        triggers.append("oom_kills")
    limit = summary.memory_limit
    if limit and stats.max > limit * MEMORY_LIMIT_PRESSURE:
        triggers.append("working_set_near_limit")
    return triggers


def _cpu_increase_triggered(config: "Config", summary: "WorkloadUsageSummary") -> bool:
    stats = summary.cpu
    request = int(summary.cpu_request or 0)
    return stats is not None and request > 0 and stats.p99 > request * _threshold(config)


def skipped_increase(config: "Config", summary: "WorkloadUsageSummary", resource: Resource) -> Optional[str]:
    """Reason an increase that the usage calls for is not proposed (``pod_level_usage_only``), or None."""
    reason = increase_skip_reason(summary)
    if reason is None:
        return None
    triggered = (
        _cpu_increase_triggered(config, summary) if resource == "cpu" else bool(_memory_triggers(config, summary))
    )
    return reason if triggered else None


def _capped(target: int, request: int, factor: Optional[float]) -> int:
    if factor is None:
        return target
    return min(target, int(math.floor(request * factor)))


def cpu_proposal(config: "Config", summary: "WorkloadUsageSummary") -> Optional[RightsizingProposal]:
    """CPU request change for one container, or None when the request fits."""
    stats = summary.cpu
    request = int(summary.cpu_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)
    throttling = summary.throttling_ratio

    if _cpu_increase_triggered(config, summary):
        if increase_skip_reason(summary):
            return None
        # A CPU request too low means contention, never a kill: one step is always capped.
        factor = max_increase_factor(config)
        raw = max(int(math.ceil(stats.p99 * config.RIGHTSIZING_HEADROOM)), request + 1)
        proposed = max(_capped(raw, request, factor), request + 1)
        proposal = RightsizingProposal(
            "cpu", "increase", request, proposed, raw, ["p99_above_request"], basis="p99", cap_factor=factor
        )
        proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling)
        return proposal

    if stats.avg / request >= config.RIGHTSIZING_CPU_THRESHOLD or not _coverage_ok(config, summary):
        return None
    raw = balanced_rightsizing_target(config, stats.avg, stats.max, stats.p95)
    proposed = max(raw, config.RECOMMENDATION_MIN_CPU_MILLICORES)
    if proposed >= request:
        return None
    proposal = RightsizingProposal("cpu", "decrease", request, proposed, raw, ["low_average_usage"], basis="balanced")
    proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling)
    return proposal


def _cpu_limit_advice(
    policy: str, summary: "WorkloadUsageSummary", proposed: int, throttling: Optional[float]
) -> List[LimitAdvice]:
    limit = summary.cpu_limit
    if not limit or limit <= 0:
        return []
    stats = summary.cpu
    p99 = stats.p99 if stats is not None else 0.0
    if policy == "remove_cpu":
        return [
            LimitAdvice(
                "cpu", "remove", "policy", limit, None, f"Remove the CPU limit ({limit}m) (limits policy remove_cpu)."
            )
        ]
    floor = max(proposed, int(math.ceil(p99 * CPU_LIMIT_P99_FACTOR)))
    if proposed > limit:
        return [
            LimitAdvice(
                "cpu",
                "raise",
                "below_request",
                limit,
                floor,
                f"The CPU limit ({limit}m) is below the proposed request: remove it or raise it to at least {floor}m.",
            )
        ]
    if throttling is not None and throttling > CPU_THROTTLING_THRESHOLD:
        return [
            LimitAdvice(
                "cpu",
                "raise",
                "throttling",
                limit,
                floor,
                f"CPU is throttled in {throttling:.0%} of periods: remove the CPU limit ({limit}m) "
                f"or raise it to at least {floor}m.",
            )
        ]
    return []


def _round_up_mib(value: float) -> int:
    # The epsilon absorbs float noise (900MiB x 1.1 is 990.0000000001MiB, not 991MiB).
    return int(math.ceil(value / MIB - 1e-6)) * MIB


def memory_proposal(config: "Config", summary: "WorkloadUsageSummary") -> Optional[RightsizingProposal]:
    """Memory request change for one container, or None when the request fits."""
    stats = summary.memory
    request = int(summary.memory_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)

    triggers = _memory_triggers(config, summary)
    if triggers:
        if increase_skip_reason(summary):
            return None
        target = stats.p99 * config.RIGHTSIZING_HEADROOM
        basis = "p99"
        if "oom_kills" in triggers or "working_set_near_limit" in triggers:
            # Peaks are known to matter (kills or limit pressure): cover the observed maximum.
            peak = stats.max * MEMORY_MAX_FACTOR
            if peak > target:
                target, basis = peak, "observed_max"
        raw = _round_up_mib(target)
        if raw <= request:
            # The request already covers usage; only the limit is at risk, and a
            # limit-only change cannot be expressed as a request recommendation.
            return None
        # OOM kills are proof that the request is too low: no cap on that step.
        factor = None if "oom_kills" in triggers else max_increase_factor(config)
        proposed = _capped(raw, request, factor)
        proposed = max((proposed // MIB) * MIB, request + MIB) if proposed < raw else proposed
        proposal = RightsizingProposal(
            "memory", "increase", request, proposed, raw, triggers, basis=basis, cap_factor=factor
        )
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
    proposal = RightsizingProposal(
        "memory", "decrease", request, proposed, raw, ["low_average_usage"], basis="balanced"
    )
    proposal.limit_advice = _memory_limit_advice(policy, summary, proposed)
    return proposal


def _memory_limit_advice(policy: str, summary: "WorkloadUsageSummary", proposed: int) -> List[LimitAdvice]:
    stats = summary.memory
    observed_max = stats.max if stats is not None else 0.0
    floor = _round_up_mib(max(float(proposed), observed_max * MEMORY_MAX_FACTOR))
    limit = summary.memory_limit
    if policy == "memory_equals_request":
        return [
            LimitAdvice(
                "memory",
                "equal_to_request",
                "policy",
                limit,
                proposed,
                f"Set the memory limit equal to the request ({proposed // MIB}MiB) "
                "(limits policy memory_equals_request).",
            )
        ]
    if limit and limit < floor:
        return [
            LimitAdvice(
                "memory",
                "raise",
                "below_floor",
                limit,
                floor,
                f"Raise the memory limit ({limit // MIB}MiB) to at least {floor // MIB}MiB: it must cover the "
                "request and 110% of the observed maximum working set.",
            )
        ]
    return []
