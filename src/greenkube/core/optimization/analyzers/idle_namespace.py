# src/greenkube/core/optimization/analyzers/idle_namespace.py
"""Idle namespace detection."""

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

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
        ns_agg: Dict[str, Dict] = defaultdict(lambda: {"joules": 0.0, "cost": 0.0})
        ns_rows: Dict[str, List[CombinedMetric]] = defaultdict(list)

        for m in context.metrics:
            ns_agg[m.namespace]["joules"] += m.joules
            ns_agg[m.namespace]["cost"] += m.total_cost
            ns_rows[m.namespace].append(m)

        for ns, agg in ns_agg.items():
            if not cfg.RECOMMEND_SYSTEM_NAMESPACES and ns in SYSTEM_NAMESPACES:
                continue
            if agg["joules"] < cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD and agg["cost"] > 0:
                # Decommissioning deletes every workload of the namespace.
                projector = ImpactProjector.for_context(context)
                delta = projector.workload_deletion(projector.workload_ref(ns, "Namespace", ns), ns_rows[ns])
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
                        **impact_fields(delta),
                    )
                )
        return recs
