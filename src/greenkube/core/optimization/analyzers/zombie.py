# src/greenkube/core/optimization/analyzers/zombie.py
"""Zombie pod detection: cost without meaningful energy consumption."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import annualized_window_total
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class ZombieAnalyzer(Analyzer):
    """Identifies targets with cost but near-zero energy consumption."""

    capability = "zombie_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config

        for (ns, target_kind, target_name), series in context.target_series().items():
            agg = {
                "total_cost": sum(metric.total_cost for metric in series),
                "joules": sum(metric.joules for metric in series),
                "co2e_grams": sum(metric.co2e_grams for metric in series),
            }
            if agg["total_cost"] > cfg.ZOMBIE_COST_THRESHOLD and agg["joules"] < cfg.ZOMBIE_ENERGY_THRESHOLD:
                label = target_label(target_kind, target_name)
                annual_cost = annualized_window_total(agg["total_cost"], context.analysis_window_seconds)
                annual_co2e = annualized_window_total(agg["co2e_grams"], context.analysis_window_seconds)
                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.ZOMBIE_POD,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} has cost ${agg['total_cost']:.4f} but consumed only "
                            f"{agg['joules']:.0f} Joules. It may be idle or a zombie."
                        ),
                        reason=(
                            f"Pod cost {agg['total_cost']:.4f} but "
                            f"consumed only {agg['joules']:.1f} Joules. "
                            f"This may be an idle or 'zombie' pod."
                        ),
                        priority="high",
                        potential_savings_cost=annual_cost,
                        potential_savings_co2e_grams=annual_co2e,
                    )
                )
        return recs
