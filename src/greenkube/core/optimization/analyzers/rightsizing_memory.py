"""Memory rightsizing: requests far above usage (decrease) or too close to it (increase)."""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.rightsizing import (
    CONTAINER_USAGE_EXCEEDS_POD,
    MEMORY_LIMIT_PRESSURE,
    MEMORY_MAX_FACTOR,
    MIB,
    OOM_SPIKE_LIMIT_FACTOR,
    RELIABILITY_SIGNALS_UNKNOWN,
    RELIABILITY_SIGNALS_UNKNOWN_TEXT,
    RightsizingProposal,
    max_increase_factor,
    memory_proposal,
    oom_proven_usage,
    oom_step_factor,
    skipped_increase,
    withheld_decrease,
)
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import POD_LEVEL

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext
    from greenkube.models.workload_usage import TargetKey, UsageStats, WorkloadUsageSummary

LOG = logging.getLogger(__name__)

_TRIGGER_TEXT = {
    "p99_above_request": "p99 working set is close to or above the request",
    "oom_kills": "containers were OOM-killed",
    "working_set_near_limit": "the working set exceeds 90% of the memory limit",
}

#: A skipped target: (target, container or None, resource, reason code).
Skip = Tuple["TargetKey", Optional[str], str, str]


_SIGNAL_LABELS = {"oom_kills": "OOM kills", "restarts": "restarts", "throttling": "CPU throttling"}


def reliability_diagnostics(skipped: List[Skip], signal_status: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Source-run diagnostics of the decreases withheld for unknown reliability signals (REC-04)."""
    withheld = [skip for skip in skipped if skip[3] == RELIABILITY_SIGNALS_UNKNOWN]
    if not withheld:
        return []
    unread = [
        f"{_SIGNAL_LABELS.get(name, name)} {status}"
        for name, status in sorted(signal_status.items())
        if name in ("oom_kills", "restarts") and status != "ok"
    ]
    detail = f" (signals: {', '.join(unread)})" if unread else ""
    return [
        {
            "level": "warning",
            "code": RELIABILITY_SIGNALS_UNKNOWN,
            "message": f"{len(withheld)} {RELIABILITY_SIGNALS_UNKNOWN_TEXT}{detail}; "
            "previous decreases of these containers are retired.",
            "count": len(withheld),
        }
    ]


def record_withheld_decrease(
    skipped: List[Skip],
    config: "Config",
    summary: "WorkloadUsageSummary",
    pod_summary: Optional["WorkloadUsageSummary"],
    resource: str,
    context: Optional["OptimizationContext"] = None,
) -> None:
    """Appends a ``reliability_signals_unknown`` skip when the target's decrease was withheld.

    The withheld target is also registered on ``context.withheld_decreases`` so
    the engine retires its previous decrease with that reason (REC-04).
    """
    reason = withheld_decrease(config, summary, pod_summary, "cpu" if resource == "cpu" else "memory")
    if reason is None:
        return
    skipped.append((summary.target, summary.container_name, resource, reason))
    if context is not None:
        context.withheld_decreases.add((*summary.target, summary.container_name or "", resource))
    LOG.info(
        "%s decrease for %s/%s/%s container %r withheld (%s): restart/OOM history could not be read.",
        "CPU" if resource == "cpu" else "Memory",
        *summary.target,
        summary.container_name,
        reason,
    )


def increase_sizing_text(
    config: "Config",
    proposal: RightsizingProposal,
    stats: "UsageStats",
    unit: str,
    stats_owner: Optional["WorkloadUsageSummary"] = None,
) -> str:
    """Explains how an increase was sized (shared wording with the CPU analyzer).

    ``stats_owner`` is the container's summary: it names the limit an OOM-sized increase starts from.
    """

    def fmt(value: float) -> str:
        return f"{value / MIB:.0f}MiB" if unit == "MiB" else f"{value:.0f}m"

    bounded = CONTAINER_USAGE_EXCEEDS_POD in proposal.evidence
    owner = "pod's" if bounded else "per-replica"
    if proposal.basis == "oom_limit":
        proven = oom_proven_usage(stats_owner) if stats_owner is not None else 0
        what = "memory limit" if stats_owner is not None and stats_owner.memory_limit else "memory request"
        step = oom_step_factor(config, stats_owner, stats.max) if stats_owner is not None else OOM_SPIKE_LIMIT_FACTOR
        if proven > 0 and stats.max >= proven * MEMORY_LIMIT_PRESSURE:
            why = (
                f"the working set reached it (observed maximum {fmt(stats.max)}), so the need above it is unknown: "
                "one decisive step instead of small ones"
            )
        else:
            why = f"a short spike the samples missed (observed maximum {fmt(stats.max)})"
        text = (
            f"Sized from the {what} ({fmt(proven)}) the OOM kills prove was passed x {step:g}, "
            f"at most {max_increase_factor(config):g}x the current request: {why}."
        )
    elif proposal.basis == "observed_max":
        text = (
            f"Sized from the {owner} observed maximum ({fmt(stats.max)}) x {MEMORY_MAX_FACTOR:g}, "
            "because OOM kills or limit pressure show that peaks matter."
        )
    else:
        text = (
            f"Sized from the {owner} p99 ({fmt(stats.p99)}, container warm-up excluded) "
            f"x headroom {config.RIGHTSIZING_HEADROOM:g}."
        )
    if bounded:
        text += (
            " The container's own usage exceeded its pod's cgroup, which contains it "
            f"({CONTAINER_USAGE_EXCEEDS_POD}): the Metrics API working set likely counts page cache, "
            "so the pod's per-replica usage bounds the sizing."
        )
    if proposal.capped and proposal.cap_factor:
        text += (
            f" Capped at {proposal.cap_factor:g}x the current request (usage suggests {fmt(proposal.raw_proposed)}): "
            "the next scan re-evaluates once this step is applied."
        )
    return text


class MemoryRightsizingAnalyzer(Analyzer):
    """Two-way memory rightsizing from per-container usage summaries (roadmap E3, E9)."""

    capability = "memory_rightsizing"

    def __init__(self, config: "Config"):
        super().__init__(config)
        #: Targets whose usage calls for a change that was not proposed, with the reason.
        self.skipped: List[Skip] = []
        self._signal_status: Dict[str, str] = {}

    def scan_diagnostics(self) -> List[Dict[str, Any]]:
        """Decreases withheld for unknown reliability signals in the last scan."""
        return reliability_diagnostics(self.skipped, self._signal_status)

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)
        self.skipped = []

        snapshot = context.usage_snapshot()
        self._signal_status = dict(snapshot.reliability_signal_status)
        for target, container_name, summary in context.iter_container_summaries():
            ns, target_kind, target_name = target
            skip = skipped_increase(cfg, summary, "memory")
            if skip is not None:
                self.skipped.append((target, container_name, "memory", skip))
                LOG.info(
                    "Memory increase for %s/%s/%s not proposed (%s): usage is only known for the whole pod.",
                    ns,
                    target_kind,
                    target_name,
                    skip,
                )
                continue
            pod_summary = snapshot.summaries.get(target, {}).get(POD_LEVEL)
            proposal = memory_proposal(cfg, summary, pod_summary)
            if proposal is None:
                record_withheld_decrease(self.skipped, cfg, summary, pod_summary, "memory", context)
            stats = proposal.stats if proposal is not None and proposal.stats is not None else summary.memory
            if proposal is None or stats is None:
                continue

            # The Impact Model prices only the moved memory reservation (BUG-15); an
            # increase is a negative cost delta, kept as such (reliability value).
            delta = projector.container_resources(
                projector.workload_ref(ns, target_kind, target_name, container_name),
                context.container_rows(target, container_name),
                memory_bytes=(proposal.current, proposal.proposed),
            )
            label = target_label(target_kind, target_name)
            if container_name:
                label = f"{label} container '{container_name}'"
            usage_ratio = stats.avg / proposal.current
            limit_text = proposal.limit_text

            if proposal.direction == "increase":
                causes = "; ".join(_TRIGGER_TEXT.get(trigger, trigger) for trigger in proposal.triggers)
                description = (
                    f"{label} uses p99 {stats.p99 / MIB:.0f}MiB and max {stats.max / MIB:.0f}MiB per replica of "
                    f"{proposal.current / MIB:.0f}MiB memory requested. "
                    f"Recommend increasing to {proposal.proposed / MIB:.0f}MiB.{limit_text}"
                )
                reason = (
                    f"Reliability: {causes}. Average usage is {usage_ratio:.0%} of the request. "
                    f"{increase_sizing_text(cfg, proposal, stats, 'MiB', summary)}"
                )
                priority = "high"
            else:
                floor_description = ""
                floor_reason = ""
                if proposal.floored:
                    min_memory_mib = cfg.RECOMMENDATION_MIN_MEMORY_BYTES // MIB
                    floor_description = f" (Floored to minimum: {min_memory_mib}MiB memory.)"
                    floor_reason = (
                        f" Recommended memory was below the minimum of {min_memory_mib}MiB; "
                        "floored to avoid impractically small requests."
                    )
                description = (
                    f"{label} uses avg {stats.avg / MIB:.0f}MiB and max {stats.max / MIB:.0f}MiB of "
                    f"{proposal.current / MIB:.0f}MiB memory requested ({usage_ratio:.0%}). "
                    f"Recommend reducing to {proposal.proposed / MIB:.0f}MiB."
                    f"{floor_description}{limit_text}"
                )
                reason = (
                    f"Average memory usage is {usage_ratio:.0%} of the request. "
                    f"P95 usage is {stats.p95 / MIB:.0f}MiB and observed max is {stats.max / MIB:.0f}MiB."
                    f"{floor_reason}{_pod_level_note(container_name)}{_jvm_note(summary.jvm_hint)}"
                )
                priority = "medium"

            recs.append(
                Recommendation(
                    pod_name=target_name,
                    container_name=container_name,
                    namespace=ns,
                    type=RecommendationType.RIGHTSIZING_MEMORY,
                    scope=scope_for_target_kind(target_kind),
                    **owner_fields(target_kind, target_name),
                    description=description,
                    reason=reason,
                    priority=priority,
                    current_memory_request_bytes=proposal.current,
                    recommended_memory_request_bytes=proposal.proposed,
                    **impact_fields(delta),
                )
            )
        return recs


def _pod_level_note(container_name: Optional[str]) -> str:
    if container_name:
        return ""
    return (
        " Pod-level usage only (no container series): the values are the pod's total request and usage; "
        "split the change between containers by their requests."
    )


def _jvm_note(hint: Optional[str]) -> str:
    if not hint:
        return ""
    return (
        f" JVM runtime suspected ({hint}): the heap is sized by the JVM, not by the working set; "
        "check -Xmx before lowering the request."
    )
