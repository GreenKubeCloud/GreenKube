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
  produces an increase: it is skipped with the reason ``pod_level_usage_only``;
* a container can never use more than its pod: when a container's statistics
  exceed its pod-level summary's (Metrics API working sets that count page
  cache, the cause of the reported 400Mi -> 1.2Gi), the increase is sized from
  the pod's statistics instead (``container_usage_exceeds_pod``).

Limits follow ``RECOMMENDATION_LIMITS_POLICY`` (``keep`` by default): limits
are left alone unless the change would make them invalid or unsafe, in which
case the recommendation states the limit it requires as a structured
:class:`LimitAdvice`. :func:`planned_limit_changes` turns that advice into the
limit edits a pull request delivers with the request change (H1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, List, Literal, Optional, Tuple

from greenkube.core.optimization.statistics import balanced_rightsizing_target, coverage_days

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.models.evidence import LimitChange
    from greenkube.models.workload_usage import UsageStats, WorkloadUsageSummary

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
#: A container statistic may exceed its pod's by this share (different sample sets) before it is bounded.
POD_CONSISTENCY_TOLERANCE = 0.05
#: Absolute slack on top of the relative tolerance: the Metrics API and cAdvisor sample at
#: different instants, so a small container can sit a mebibyte (or a few millicores) above
#: its pod without contradicting it (e2e: 6.7MiB container in a 5.8MiB pod blocked a
#: 384Mi -> 16Mi decrease).
POD_CONSISTENCY_SLACK = {"cpu": 5.0, "memory": 4.0 * MIB}
#: Evidence flag of an increase sized from the pod's statistics because the container's contradicted them.
CONTAINER_USAGE_EXCEEDS_POD = "container_usage_exceeds_pod"

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
    #: Statistics the change was sized from (the container's, or bounded by its pod's).
    stats: Optional["UsageStats"] = None
    #: Evidence flags about the sizing data (``container_usage_exceeds_pod``).
    evidence: List[str] = field(default_factory=list)

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


def bounded_by_pod(
    stats: Optional["UsageStats"], pod_stats: Optional["UsageStats"], slack: float = 0.0
) -> Tuple[Optional["UsageStats"], bool]:
    """A container's increase statistics, bounded by its pod-level statistics.

    A container runs inside its pod's cgroup: per replica, its usage can never
    exceed the pod's. When the container's p99 or maximum is above the pod's by
    more than :data:`POD_CONSISTENCY_TOLERANCE` plus ``slack`` (absolute, in the
    statistic's unit), the container data contradicts the pod cgroup (e.g. a
    Metrics API working set that counts page cache), so every statistic is
    bounded by the pod's. Returns ``(stats, bounded)``.
    """
    if stats is None or pod_stats is None or pod_stats.sample_count <= 0:
        return stats, False
    tolerance = 1 + POD_CONSISTENCY_TOLERANCE
    if stats.p99 <= pod_stats.p99 * tolerance + slack and stats.max <= pod_stats.max * tolerance + slack:
        return stats, False
    bounded = replace(
        stats,
        avg=min(stats.avg, pod_stats.avg),
        p50=min(stats.p50, pod_stats.p50),
        p90=min(stats.p90, pod_stats.p90),
        p95=min(stats.p95, pod_stats.p95),
        p99=min(stats.p99, pod_stats.p99),
        max=min(stats.max, pod_stats.max),
    )
    return bounded, True


def _increase_stats(
    summary: "WorkloadUsageSummary", pod_summary: Optional["WorkloadUsageSummary"], resource: Resource
) -> Tuple[Optional["UsageStats"], List[str]]:
    stats = summary.cpu if resource == "cpu" else summary.memory
    if not summary.container_name or pod_summary is None or pod_summary is summary:
        return stats, []
    pod_stats = pod_summary.cpu if resource == "cpu" else pod_summary.memory
    bounded, was_bounded = bounded_by_pod(stats, pod_stats, POD_CONSISTENCY_SLACK[resource])
    return bounded, [CONTAINER_USAGE_EXCEEDS_POD] if was_bounded else []


def _memory_triggers(
    config: "Config", summary: "WorkloadUsageSummary", stats: Optional["UsageStats"] = None
) -> List[str]:
    stats = summary.memory if stats is None else stats
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


def _cpu_increase_triggered(
    config: "Config", summary: "WorkloadUsageSummary", stats: Optional["UsageStats"] = None
) -> bool:
    stats = summary.cpu if stats is None else stats
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


def cpu_proposal(
    config: "Config",
    summary: "WorkloadUsageSummary",
    pod_summary: Optional["WorkloadUsageSummary"] = None,
) -> Optional[RightsizingProposal]:
    """CPU request change for one container, or None when the request fits.

    ``pod_summary`` is the target's pod-level summary: an increase is never
    sized above what the pod itself used (:func:`bounded_by_pod`).
    """
    stats = summary.cpu
    request = int(summary.cpu_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)
    throttling = summary.throttling_ratio
    increase_stats, evidence = _increase_stats(summary, pod_summary, "cpu")

    if increase_stats is not None and _cpu_increase_triggered(config, summary, increase_stats):
        if increase_skip_reason(summary):
            return None
        # A CPU request too low means contention, never a kill: one step is always capped.
        factor = max_increase_factor(config)
        raw = max(int(math.ceil(increase_stats.p99 * config.RIGHTSIZING_HEADROOM)), request + 1)
        proposed = max(_capped(raw, request, factor), request + 1)
        proposal = RightsizingProposal(
            "cpu",
            "increase",
            request,
            proposed,
            raw,
            ["p99_above_request"],
            basis="p99",
            cap_factor=factor,
            stats=increase_stats,
            evidence=evidence,
        )
        proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling, increase_stats)
        return proposal
    if evidence or _cpu_increase_triggered(config, summary):
        # The container's own data contradicts its pod's: no decrease from it either (fail closed).
        return None

    if stats.avg / request >= config.RIGHTSIZING_CPU_THRESHOLD or not _coverage_ok(config, summary):
        return None
    raw = balanced_rightsizing_target(config, stats.avg, stats.max, stats.p95)
    proposed = max(raw, config.RECOMMENDATION_MIN_CPU_MILLICORES)
    if proposed >= request:
        return None
    proposal = RightsizingProposal(
        "cpu", "decrease", request, proposed, raw, ["low_average_usage"], basis="balanced", stats=stats
    )
    proposal.limit_advice = _cpu_limit_advice(policy, summary, proposed, throttling)
    return proposal


def _cpu_limit_advice(
    policy: str,
    summary: "WorkloadUsageSummary",
    proposed: int,
    throttling: Optional[float],
    stats: Optional["UsageStats"] = None,
) -> List[LimitAdvice]:
    limit = summary.cpu_limit
    if not limit or limit <= 0:
        return []
    stats = summary.cpu if stats is None else stats
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


def memory_proposal(
    config: "Config",
    summary: "WorkloadUsageSummary",
    pod_summary: Optional["WorkloadUsageSummary"] = None,
) -> Optional[RightsizingProposal]:
    """Memory request change for one container, or None when the request fits.

    ``pod_summary`` is the target's pod-level summary: an increase is never
    sized above what the pod itself used (:func:`bounded_by_pod`).
    """
    stats = summary.memory
    request = int(summary.memory_request or 0)
    if stats is None or request <= 0:
        return None
    policy = limits_policy(config)
    increase_stats, evidence = _increase_stats(summary, pod_summary, "memory")

    triggers = _memory_triggers(config, summary, increase_stats)
    if triggers and increase_stats is not None:
        if increase_skip_reason(summary):
            return None
        target = increase_stats.p99 * config.RIGHTSIZING_HEADROOM
        basis = "p99"
        if "oom_kills" in triggers or "working_set_near_limit" in triggers:
            # Peaks are known to matter (kills or limit pressure): cover the observed maximum.
            peak = increase_stats.max * MEMORY_MAX_FACTOR
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
            "memory",
            "increase",
            request,
            proposed,
            raw,
            triggers,
            basis=basis,
            cap_factor=factor,
            stats=increase_stats,
            evidence=evidence,
        )
        proposal.limit_advice = _memory_limit_advice(policy, summary, proposed, increase_stats)
        return proposal
    if evidence or _memory_triggers(config, summary):
        # The container's own data contradicts its pod's: no decrease from it either (fail closed).
        return None

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
        "memory", "decrease", request, proposed, raw, ["low_average_usage"], basis="balanced", stats=stats
    )
    proposal.limit_advice = _memory_limit_advice(policy, summary, proposed)
    return proposal


def _memory_limit_advice(
    policy: str, summary: "WorkloadUsageSummary", proposed: int, stats: Optional["UsageStats"] = None
) -> List[LimitAdvice]:
    stats = summary.memory if stats is None else stats
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


def planned_limit_changes(
    config: "Config",
    summary: "WorkloadUsageSummary",
    pod_summary: Optional["WorkloadUsageSummary"],
    resource: Resource,
    proposed_request: int,
) -> List["LimitChange"]:
    """Limit changes delivered with a request change of ``resource`` (H1).

    Recomputed against the final proposed request (consensus may restate the
    analyzer's value), from the same :class:`LimitAdvice` rules:

    * an **increase** (reliability) carries every limit change it needs: a CPU
      limit below the new request or throttled is raised to the advised floor
      (or removed under ``remove_cpu``), a memory limit below the request or
      110% of the observed maximum is raised to that floor. An increase never
      lowers an existing limit: under ``memory_equals_request`` the memory limit
      becomes ``max(request, 110% of the maximum)`` only when that raises it;
    * a **decrease** carries the limits policy's own changes only
      (``remove_cpu``, ``memory_equals_request``), so with the default ``keep``
      a decrease leaves limits untouched;
    * a set limit is never below the proposed request, and a memory limit is
      never removed.
    """
    from greenkube.models.evidence import LimitChange

    current_request = int((summary.cpu_request if resource == "cpu" else summary.memory_request) or 0)
    if proposed_request <= 0 or current_request <= 0 or proposed_request == current_request:
        return []
    increase = proposed_request > current_request
    policy = limits_policy(config)
    stats, _ = _increase_stats(summary, pod_summary, resource) if increase else (None, [])
    if resource == "cpu":
        advice = _cpu_limit_advice(policy, summary, proposed_request, summary.throttling_ratio, stats)
    else:
        advice = _memory_limit_advice(policy, summary, proposed_request, stats)
    current_limit = summary.cpu_limit if resource == "cpu" else summary.memory_limit
    current_limit = int(current_limit) if current_limit else None
    changes: List[LimitChange] = []
    for item in advice:
        if not increase and item.reason != "policy":
            continue
        if item.action == "remove":
            if resource != "cpu" or current_limit is None:
                continue
            changes.append(LimitChange(resource="cpu", action="remove", current=current_limit, reason=item.reason))
            continue
        target = item.minimum
        if target is None:
            continue
        if increase and item.action == "equal_to_request" and resource == "memory":
            floor_stats = stats if stats is not None else summary.memory
            peak = floor_stats.max * MEMORY_MAX_FACTOR if floor_stats is not None else 0.0
            target = max(target, _round_up_mib(peak))
        if target < proposed_request:
            continue
        if increase and current_limit is not None and target <= current_limit:
            continue
        if current_limit is not None and target == current_limit:
            continue
        changes.append(
            LimitChange(
                resource=resource, action="set", current=current_limit, proposed=int(target), reason=item.reason
            )
        )
    return changes
