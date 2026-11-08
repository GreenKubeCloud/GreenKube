"""Memory rightsizing: requests far above usage (decrease) or too close to it (increase)."""

import logging
from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.rightsizing import MIB, memory_proposal
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

LOG = logging.getLogger(__name__)

_TRIGGER_TEXT = {
    "p99_above_request": "p99 working set is close to or above the request",
    "oom_kills": "containers were OOM-killed",
    "working_set_near_limit": "the working set exceeds 90% of the memory limit",
}


class MemoryRightsizingAnalyzer(Analyzer):
    """Two-way memory rightsizing from per-container usage summaries (roadmap E3, E9)."""

    capability = "memory_rightsizing"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)

        for target, container_name, summary in context.iter_container_summaries():
            ns, target_kind, target_name = target
            proposal = memory_proposal(cfg, summary)
            stats = summary.memory
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
            limit_text = "".join(f" {advice}" for advice in proposal.limit_advice)

            if proposal.direction == "increase":
                causes = "; ".join(_TRIGGER_TEXT.get(trigger, trigger) for trigger in proposal.triggers)
                description = (
                    f"{label} uses p99 {stats.p99 / MIB:.0f}MiB and max {stats.max / MIB:.0f}MiB of "
                    f"{proposal.current / MIB:.0f}MiB memory requested. "
                    f"Recommend increasing to {proposal.proposed / MIB:.0f}MiB.{limit_text}"
                )
                reason = f"Reliability: {causes}. Average usage is {usage_ratio:.0%} of the request."
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
                    f"{floor_reason}"
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
