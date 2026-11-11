"""Memory rightsizing: requests far above usage (decrease) or too close to it (increase)."""

import logging
from typing import TYPE_CHECKING, List, Optional, Tuple

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.rightsizing import (
    CONTAINER_USAGE_EXCEEDS_POD,
    MEMORY_MAX_FACTOR,
    MIB,
    RightsizingProposal,
    memory_proposal,
    skipped_increase,
)
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import POD_LEVEL

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext
    from greenkube.models.workload_usage import TargetKey, UsageStats

LOG = logging.getLogger(__name__)

_TRIGGER_TEXT = {
    "p99_above_request": "p99 working set is close to or above the request",
    "oom_kills": "containers were OOM-killed",
    "working_set_near_limit": "the working set exceeds 90% of the memory limit",
}

#: A skipped target: (target, container or None, resource, reason code).
Skip = Tuple["TargetKey", Optional[str], str, str]


def increase_sizing_text(config: "Config", proposal: RightsizingProposal, stats: "UsageStats", unit: str) -> str:
    """Explains how an increase was sized (shared wording with the CPU analyzer)."""

    def fmt(value: float) -> str:
        return f"{value / MIB:.0f}MiB" if unit == "MiB" else f"{value:.0f}m"

    bounded = CONTAINER_USAGE_EXCEEDS_POD in proposal.evidence
    owner = "pod's" if bounded else "per-replica"
    if proposal.basis == "observed_max":
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

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)
        self.skipped = []

        snapshot = context.usage_snapshot()
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
                    f"{increase_sizing_text(cfg, proposal, stats, 'MiB')}"
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
