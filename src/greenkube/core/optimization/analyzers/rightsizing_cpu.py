"""CPU rightsizing: requests far above usage (decrease) or too close to it (increase)."""

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.analyzers.rightsizing import (
    RELIABILITY_SIGNALS_UNKNOWN,
    cpu_proposal,
    skipped_increase,
)
from greenkube.core.optimization.analyzers.rightsizing_memory import (
    Skip,
    _pod_level_note,
    increase_sizing_text,
    record_withheld_decrease,
    reliability_diagnostics,
)
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import POD_LEVEL

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

LOG = logging.getLogger(__name__)


class CpuRightsizingAnalyzer(Analyzer):
    """Two-way CPU rightsizing from per-container usage summaries (roadmap E3, E9)."""

    capability = "cpu_rightsizing"

    def __init__(self, config: "Config"):
        super().__init__(config)
        #: Targets whose usage calls for a change that was not proposed, with the reason.
        self.skipped: List[Skip] = []
        self._signal_status: Dict[str, str] = {}

    def scan_diagnostics(self) -> List[Dict[str, Any]]:
        """Decreases withheld for unknown reliability signals in the last scan."""
        return reliability_diagnostics(self.skipped, self._signal_status)

    def scan_complete(self) -> bool:
        """Incomplete when a decrease was withheld: previous advice is kept (REC-04)."""
        return not any(skip[3] == RELIABILITY_SIGNALS_UNKNOWN for skip in self.skipped)

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)
        self.skipped = []

        snapshot = context.usage_snapshot()
        self._signal_status = dict(snapshot.reliability_signal_status)
        for target, container_name, summary in context.iter_container_summaries():
            ns, target_kind, target_name = target
            skip = skipped_increase(cfg, summary, "cpu")
            if skip is not None:
                self.skipped.append((target, container_name, "cpu", skip))
                LOG.info(
                    "CPU increase for %s/%s/%s not proposed (%s): usage is only known for the whole pod.",
                    ns,
                    target_kind,
                    target_name,
                    skip,
                )
                continue
            pod_summary = snapshot.summaries.get(target, {}).get(POD_LEVEL)
            proposal = cpu_proposal(cfg, summary, pod_summary)
            if proposal is None:
                record_withheld_decrease(self.skipped, cfg, summary, pod_summary, "cpu")
            stats = proposal.stats if proposal is not None and proposal.stats is not None else summary.cpu
            if proposal is None or stats is None:
                continue

            # The Impact Model prices only the moved CPU reservation (BUG-15); an
            # increase is a negative cost delta, kept as such (reliability value).
            delta = projector.container_resources(
                projector.workload_ref(ns, target_kind, target_name, container_name),
                context.container_rows(target, container_name),
                cpu_millicores=(proposal.current, proposal.proposed),
            )
            label = target_label(target_kind, target_name)
            if container_name:
                label = f"{label} container '{container_name}'"
            usage_ratio = stats.avg / proposal.current
            limit_text = proposal.limit_text

            if proposal.direction == "increase":
                description = (
                    f"{label} uses p99 {stats.p99:.0f}m and max {stats.max:.0f}m per replica of "
                    f"{proposal.current}m CPU requested. Recommend increasing to {proposal.proposed}m.{limit_text}"
                )
                reason = (
                    f"Reliability: p99 CPU usage ({stats.p99:.0f}m) is above "
                    f"{cfg.RIGHTSIZING_INCREASE_THRESHOLD:.0%} of the request; the workload competes for CPU "
                    f"under contention. Average usage is {usage_ratio:.0%} of the request. "
                    f"{increase_sizing_text(cfg, proposal, stats, 'm')}"
                )
                priority = "high" if stats.p99 > proposal.current else "medium"
            else:
                floor_description = ""
                floor_reason = ""
                if proposal.floored:
                    floor_description = f" (Floored to minimum: {cfg.RECOMMENDATION_MIN_CPU_MILLICORES}m CPU.)"
                    floor_reason = (
                        f" Recommended value was below the minimum of "
                        f"{cfg.RECOMMENDATION_MIN_CPU_MILLICORES}m; "
                        "floored to avoid impractically small requests."
                    )
                description = (
                    f"{label} uses avg {stats.avg:.0f}m and max {stats.max:.0f}m of {proposal.current}m CPU "
                    f"requested ({usage_ratio:.0%}). Recommend reducing to {proposal.proposed}m."
                    f"{floor_description}{limit_text}"
                )
                reason = (
                    f"Average CPU usage is {usage_ratio:.0%} of the request. "
                    f"P95 usage is {stats.p95:.0f}m and observed max is {stats.max:.0f}m."
                    f"{floor_reason}{_pod_level_note(container_name)}"
                )
                priority = "medium"

            recs.append(
                Recommendation(
                    pod_name=target_name,
                    container_name=container_name,
                    namespace=ns,
                    type=RecommendationType.RIGHTSIZING_CPU,
                    scope=scope_for_target_kind(target_kind),
                    **owner_fields(target_kind, target_name),
                    description=description,
                    reason=reason,
                    priority=priority,
                    current_cpu_request_millicores=proposal.current,
                    recommended_cpu_request_millicores=proposal.proposed,
                    **impact_fields(delta),
                )
            )
        return recs
