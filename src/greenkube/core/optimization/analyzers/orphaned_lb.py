# src/greenkube/core/optimization/analyzers/orphaned_lb.py
"""Orphaned LoadBalancer Service cleanup recommendations."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class OrphanedLBAnalyzer(Analyzer):
    """Identifies LoadBalancer Services that have no backing endpoints."""

    capability = "lb_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        load_balancers = context.load_balancers
        if not load_balancers:
            return recs

        cfg = self.config

        for lb in load_balancers:
            name = getattr(lb, "name", None)
            if not name:
                continue

            namespace = getattr(lb, "namespace", None) or ""
            endpoint_count = getattr(lb, "endpoint_count", 0) or 0
            external_ip = getattr(lb, "external_ip", None) or ""
            ports = getattr(lb, "ports", "") or ""

            external_label = f" ({external_ip})" if external_ip else ""
            ports_label = f" Ports: {ports}." if ports else ""

            # CO2e savings are intentionally not projected: energy estimation
            # currently only covers CPU usage, not cloud LoadBalancer overhead.
            annual_cost = getattr(lb, "annual_cost", None)
            if not isinstance(annual_cost, (int, float)) or annual_cost <= 0:
                annual_cost = None
            description_savings = ""
            if annual_cost is not None:
                description_savings = f" OpenCost-reported cost savings: ${annual_cost:.2f}/year."
            else:
                annual_cost = cfg.LOAD_BALANCER_COST_PER_MONTH * 12
                description_savings = (
                    f" Estimated cost savings: ${annual_cost:.2f}/year "
                    f"(at ${cfg.LOAD_BALANCER_COST_PER_MONTH:.2f}/month)."
                )

            recs.append(
                Recommendation(
                    pod_name=name,
                    namespace=namespace or None,
                    type=RecommendationType.ORPHANED_LOAD_BALANCER,
                    scope="cluster",
                    description=(
                        f"LoadBalancer Service '{name}' in namespace '{namespace or 'unknown'}' has no "
                        f"backing endpoints ({endpoint_count} ready){external_label}. Deleting it removes "
                        f"the cloud LoadBalancer and stops its hourly billing.{ports_label}"
                        f"{description_savings}"
                    ),
                    reason=(
                        f"The Service of type LoadBalancer has {endpoint_count} ready endpoints, so the "
                        f"provisioned cloud LoadBalancer routes traffic to nothing."
                    ),
                    priority="medium",
                    potential_savings_cost=annual_cost,
                )
            )
        return recs
