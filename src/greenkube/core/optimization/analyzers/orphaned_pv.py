# src/greenkube/core/optimization/analyzers/orphaned_pv.py
"""Orphaned PersistentVolume cleanup recommendations."""

from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.impact.types import CostSource
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class OrphanedPVAnalyzer(Analyzer):
    """Identifies PersistentVolumes whose claims are gone or released."""

    capability = "storage_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        persistent_volumes = context.persistent_volumes
        if not persistent_volumes:
            return recs

        cfg = self.config

        for pv in persistent_volumes:
            name = getattr(pv, "name", None)
            if not name:
                continue

            phase = getattr(pv, "phase", "") or ""
            capacity_bytes = getattr(pv, "capacity_bytes", 0) or 0
            claim_namespace = getattr(pv, "claim_namespace", None)
            claim_name = getattr(pv, "claim_name", None)
            reclaim_policy = getattr(pv, "reclaim_policy", "") or ""

            capacity_gib = capacity_bytes / (1024**3)
            claim_label = f"{claim_namespace}/{claim_name}" if claim_namespace and claim_name else "unknown claim"

            if phase.lower() == "released":
                reason = (
                    f"The PV is in 'Released' phase: its PVC {claim_label} was deleted but the "
                    f"volume was not reclaimed (reclaim policy: {reclaim_policy or 'unknown'})."
                )
            else:
                reason = (
                    f"The PV references PVC {claim_label} which no longer exists in the cluster, "
                    f"so the volume can never be bound or used."
                )

            # Deletion releases the asset's current allocated rate (realization 1).
            # CO2e is not projected: the energy model does not cover storage.
            annual_cost = getattr(pv, "annual_cost", None)
            if not isinstance(annual_cost, (int, float)) or annual_cost <= 0:
                annual_cost = None
            description_savings = ""
            cost_source = CostSource.OPENCOST
            cost_detail = "opencost_pv_cost"
            if annual_cost is not None and capacity_bytes > 0:
                description_savings = f" OpenCost-reported cost savings: ${annual_cost:.2f}/year."
            elif capacity_bytes > 0:
                annual_cost = capacity_gib * cfg.STORAGE_COST_PER_GIB_MONTH * 12
                cost_source = CostSource.CUSTOM_RATE
                cost_detail = "STORAGE_COST_PER_GIB_MONTH"
                description_savings = (
                    f" Estimated cost savings: ${annual_cost:.2f}/year "
                    f"(at ${cfg.STORAGE_COST_PER_GIB_MONTH:.2f}/GiB-month)."
                )
            savings_fields: dict = {"potential_savings_cost": None}
            if annual_cost is not None:
                savings_fields = impact_fields(
                    ImpactProjector.for_context(context).asset_deletion(
                        asset_kind="persistent_volume",
                        allocated_cost_annual=annual_cost,
                        component="storage",
                        cost_source=cost_source,
                        detail=cost_detail,
                    )
                )

            recs.append(
                Recommendation(
                    pod_name=name,
                    type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
                    scope="cluster",
                    description=(
                        f"PersistentVolume '{name}' is orphaned ({capacity_gib:.1f}GiB, "
                        f"phase {phase or 'unknown'}, reclaim policy {reclaim_policy or 'unknown'}). "
                        f"Deleting it releases {capacity_gib:.1f}GiB of provisioned storage."
                        f"{description_savings}"
                    ),
                    reason=reason,
                    priority="medium",
                    **savings_fields,
                )
            )
        return recs
