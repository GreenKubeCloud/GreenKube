# src/greenkube/core/optimization/analyzers/orphaned_lb.py
"""Orphaned LoadBalancer Service cleanup recommendations."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.impact.types import CostSource
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

            # Deletion releases the asset's current allocated rate (realization 1).
            # CO2e is not projected: the energy model does not cover load balancers.
            annual_cost = getattr(lb, "annual_cost", None)
            if not isinstance(annual_cost, (int, float)) or annual_cost <= 0:
                annual_cost = None
            description_savings = ""
            cost_source = CostSource.OPENCOST
            cost_detail = "opencost_lb_cost"
            if annual_cost is not None:
                description_savings = f" OpenCost-reported cost savings: ${annual_cost:.2f}/year."
            else:
                annual_cost = cfg.LOAD_BALANCER_COST_PER_MONTH * 12
                cost_source = CostSource.CUSTOM_RATE
                cost_detail = "LOAD_BALANCER_COST_PER_MONTH"
                description_savings = (
                    f" Estimated cost savings: ${annual_cost:.2f}/year "
                    f"(at ${cfg.LOAD_BALANCER_COST_PER_MONTH:.2f}/month)."
                )
            delta = ImpactProjector.for_context(context).asset_deletion(
                asset_kind="load_balancer",
                allocated_cost_annual=annual_cost,
                component="lb",
                cost_source=cost_source,
                detail=cost_detail,
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
                    **impact_fields(delta),
                )
            )
        return recs
