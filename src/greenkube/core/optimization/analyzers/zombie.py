"""Zombie pod detection: cost without meaningful energy consumption."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class ZombieAnalyzer(Analyzer):
    """Identifies targets with cost but near-zero energy consumption."""

    capability = "zombie_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)

        for target, summary in context.workload_summaries():
            ns, target_kind, target_name = target
            total_cost = summary.total_cost
            joules = summary.joules
            if total_cost > cfg.ZOMBIE_COST_THRESHOLD and joules < cfg.ZOMBIE_ENERGY_THRESHOLD:
                label = target_label(target_kind, target_name)
                # Deleting the workload releases its whole allocation: observed cost,
                # plus the idle, dynamic and embodied CO2e share it holds.
                delta = projector.workload_deletion(
                    projector.workload_ref(ns, target_kind, target_name), context.workload_rows(target)
                )
                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.ZOMBIE_POD,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} has cost ${total_cost:.4f} but consumed only "
                            f"{joules:.0f} Joules. It may be idle or a zombie."
                        ),
                        reason=(
                            f"Pod cost {total_cost:.4f} but "
                            f"consumed only {joules:.1f} Joules. "
                            f"This may be an idle or 'zombie' pod."
                        ),
                        priority="high",
                        **impact_fields(delta),
                    )
                )
        return recs
