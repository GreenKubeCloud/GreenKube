# src/greenkube/core/optimization/analyzers/idle_namespace.py
"""Idle namespace detection."""

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import annualized_window_total
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

SYSTEM_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "coredns",
    "istio-system",
    "kubernetes-dashboard",
}


class IdleNamespaceAnalyzer(Analyzer):
    """Identifies namespaces with minimal total activity."""

    capability = "namespace_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        ns_agg: Dict[str, Dict] = defaultdict(lambda: {"joules": 0.0, "cost": 0.0, "co2e": 0.0})

        for m in context.metrics:
            ns_agg[m.namespace]["joules"] += m.joules
            ns_agg[m.namespace]["cost"] += m.total_cost
            ns_agg[m.namespace]["co2e"] += m.co2e_grams

        for ns, agg in ns_agg.items():
            if not cfg.RECOMMEND_SYSTEM_NAMESPACES and ns in SYSTEM_NAMESPACES:
                continue
            if agg["joules"] < cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD and agg["cost"] > 0:
                annual_cost = annualized_window_total(agg["cost"], context.analysis_window_seconds)
                annual_co2e = annualized_window_total(agg["co2e"], context.analysis_window_seconds)
                recs.append(
                    Recommendation(
                        namespace=ns,
                        type=RecommendationType.IDLE_NAMESPACE,
                        scope="namespace",
                        description=(
                            f"Namespace '{ns}' consumed only {agg['joules']:.0f}J total energy "
                            f"but costs ${agg['cost']:.4f}. Consider decommissioning."
                        ),
                        reason=(
                            f"Total namespace energy ({agg['joules']:.0f}J) is below the "
                            f"idle threshold ({cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD}J)."
                        ),
                        priority="low",
                        potential_savings_cost=annual_cost,
                        potential_savings_co2e_grams=annual_co2e,
                    )
                )
        return recs
