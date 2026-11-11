"""Zombie workload detection: cost without meaningful energy consumption.

A zombie recommendation suggests deleting a workload, so it is only emitted
with evidence (cost, energy and the observed span) and never for workloads
that are idle by design: node agents (DaemonSets), operators, controllers and
webhooks that wait for events, workloads of infrastructure namespaces, and
namespaces opted out with ``greenkube.io/ignore``. The low-energy condition
must cover at least ``DELETION_MIN_UNUSED_DAYS`` of observations.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, List, Optional

from greenkube.core.deletion_safety import (
    NODE_AGENT_KINDS,
    format_days,
    is_infrastructure_namespace,
    looks_event_driven,
    min_unused_period,
    opt_out_reason,
)
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.models.workload_usage import WorkloadUsageSummary

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


def observed_span(summary: WorkloadUsageSummary) -> Optional[timedelta]:
    """Span of hours the summary covers (first to last observed hour, inclusive)."""
    if summary.first_hour is None or summary.last_hour is None:
        return None
    return summary.last_hour - summary.first_hour + timedelta(hours=1)


def namespace_opt_out(context: "OptimizationContext", namespace: str) -> Optional[str]:
    """The opt-out annotation of a namespace, when its metadata was read."""
    info = (context.namespace_info or {}).get(namespace)
    return opt_out_reason(getattr(info, "metadata", None)) if info is not None else None


class ZombieAnalyzer(Analyzer):
    """Identifies targets with cost but near-zero energy consumption, with evidence."""

    capability = "zombie_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)
        minimum = min_unused_period(cfg)

        for target, summary in context.workload_summaries():
            ns, target_kind, target_name = target
            total_cost = summary.total_cost
            joules = summary.joules
            if not (total_cost > cfg.ZOMBIE_COST_THRESHOLD and joules < cfg.ZOMBIE_ENERGY_THRESHOLD):
                continue
            # Idle by design or protected: never a deletion candidate.
            if (
                is_infrastructure_namespace(ns)
                or target_kind in NODE_AGENT_KINDS
                or looks_event_driven(target_name)
                or namespace_opt_out(context, ns)
            ):
                continue
            span = observed_span(summary)
            if span is None or span < minimum:
                continue

            label = target_label(target_kind, target_name)
            # Deleting the workload releases its whole allocation: observed cost,
            # plus the idle, dynamic and embodied CO2e share it holds.
            delta = projector.workload_deletion(
                projector.workload_ref(ns, target_kind, target_name), context.workload_rows(target)
            )
            evidence = [
                f"cost ${total_cost:.4f} over the lookback",
                f"only {joules:.1f} J of energy (threshold {cfg.ZOMBIE_ENERGY_THRESHOLD:.0f} J)",
                f"observed over {format_days(span)} ({summary.hours_observed} hour(s) with samples)",
                "not a DaemonSet, operator, controller or webhook, and not in an infrastructure namespace",
            ]
            recs.append(
                Recommendation(
                    pod_name=target_name,
                    namespace=ns,
                    type=RecommendationType.ZOMBIE_POD,
                    scope=scope_for_target_kind(target_kind),
                    **owner_fields(target_kind, target_name),
                    description=(
                        f"{label} appears idle: it cost ${total_cost:.4f} but consumed only "
                        f"{joules:.0f} Joules over {format_days(span)}. Review whether it is still needed "
                        f"before removing it (standby replicas and event-driven services look idle too)."
                    ),
                    reason=(
                        f"Evidence that it is unused: {'; '.join(evidence)}. GreenKube does not see incoming "
                        f"traffic or external consumers; annotate the namespace with "
                        f'greenkube.io/ignore="true" to stop these recommendations.'
                    ),
                    priority="high",
                    **impact_fields(delta),
                )
            )
        return recs
