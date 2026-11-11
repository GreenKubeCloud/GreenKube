# src/greenkube/core/optimization/analyzers/orphaned_lb.py
"""Cleanup recommendations for LoadBalancer Services that appear unused.

The collector (``greenkube.collectors.lb_collector``) only reports Services
it can prove unused, with the evidence; this analyzer additionally requires
the condition to have persisted for ``DELETION_MIN_UNUSED_DAYS`` and words
the recommendation as a review ("appears unused"), never as a certainty.

An address from a bare-metal allocator (MetalLB, Cilium LB-IPAM, kube-vip)
has no cloud bill: unless OpenCost reports a cost for it, the recommendation
projects no saving and says why.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

from greenkube.core.deletion_safety import format_days, min_unused_period
from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.impact.types import CostSource
from greenkube.models.metrics import Recommendation, RecommendationType
from greenkube.utils.time import ensure_utc

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class OrphanedLBAnalyzer(Analyzer):
    """Identifies LoadBalancer Services that have looked unused long enough, with evidence."""

    capability = "lb_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        load_balancers = context.load_balancers
        if not load_balancers:
            return recs

        cfg = self.config
        now = ensure_utc(context.window_end or datetime.now(timezone.utc))
        minimum = min_unused_period(cfg)

        for lb in load_balancers:
            name = getattr(lb, "name", None)
            if not name:
                continue
            evidence = [str(item) for item in (getattr(lb, "evidence", None) or []) if item]
            first_seen = getattr(lb, "first_seen_at", None)
            # No evidence or no proven duration: nothing to recommend (fail closed).
            if not evidence or not isinstance(first_seen, datetime):
                continue
            unused_for = now - ensure_utc(first_seen)
            if unused_for < minimum:
                continue

            namespace = getattr(lb, "namespace", None) or ""
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
            allocator = getattr(lb, "bare_metal_allocator", None)
            releases = "releases the LoadBalancer and stops its billing"
            if annual_cost is not None:
                description_savings = f" OpenCost-reported cost savings: ${annual_cost:.2f}/year."
            elif allocator:
                # A bare-metal allocator hands out addresses from a local pool: deleting
                # the Service frees an address, not a billed cloud load balancer.
                annual_cost = 0.0
                releases = f"returns its address to the {allocator} pool"
                cost_source = CostSource.NONE
                cost_detail = f"bare_metal_load_balancer:{allocator}"
                description_savings = (
                    f" No cost saving: its address comes from {allocator}, a bare-metal allocator "
                    "with no cloud load-balancer billing."
                )
            elif not external_ip:
                # Pending: no controller ever assigned an address (no cloud controller,
                # an empty bare-metal pool), so no billed load balancer exists.
                annual_cost = 0.0
                releases = "removes a LoadBalancer request that was never provisioned"
                cost_source = CostSource.NONE
                cost_detail = "load_balancer_without_address"
                description_savings = (
                    " No cost saving: it has no external address, so no load balancer was provisioned for it."
                )
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

            since = ensure_utc(first_seen).strftime("%Y-%m-%d %H:%M UTC")
            evidence_text = "; ".join(evidence + [f"unused continuously since {since} ({format_days(unused_for)})"])
            recs.append(
                Recommendation(
                    pod_name=name,
                    namespace=namespace or None,
                    type=RecommendationType.ORPHANED_LOAD_BALANCER,
                    scope="cluster",
                    description=(
                        f"LoadBalancer Service '{name}' in namespace '{namespace or 'unknown'}'{external_label} "
                        f"appears unused for {format_days(unused_for)}. Review it before deleting: deleting it "
                        f"{releases}.{ports_label}{description_savings}"
                    ),
                    reason=(
                        f"Evidence that it is unused: {evidence_text}. Add the annotation "
                        f'greenkube.io/ignore="true" to the Service if it is still needed.'
                    ),
                    priority="medium",
                    **impact_fields(delta),
                )
            )
        return recs
