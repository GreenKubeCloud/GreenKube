"""Idle namespace detection."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
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

        for ns, summaries in context.namespace_targets().items():
            if not cfg.RECOMMEND_SYSTEM_NAMESPACES and ns in SYSTEM_NAMESPACES:
                continue
            joules = sum(summary.joules for summary in summaries)
            cost = sum(summary.total_cost for summary in summaries)
            if joules < cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD and cost > 0:
                # Decommissioning deletes every workload of the namespace.
                projector = ImpactProjector.for_context(context)
                rows = [row for row in context.metrics if row.namespace == ns and not row.container_name]
                delta = projector.workload_deletion(projector.workload_ref(ns, "Namespace", ns), rows)
                recs.append(
                    Recommendation(
                        namespace=ns,
                        type=RecommendationType.IDLE_NAMESPACE,
                        scope="namespace",
                        description=(
                            f"Namespace '{ns}' consumed only {joules:.0f}J total energy "
                            f"but costs ${cost:.4f}. Consider decommissioning."
                        ),
                        reason=(
                            f"Total namespace energy ({joules:.0f}J) is below the "
                            f"idle threshold ({cfg.IDLE_NAMESPACE_ENERGY_THRESHOLD}J)."
                        ),
                        priority="low",
                        **impact_fields(delta),
                    )
                )
        return recs
