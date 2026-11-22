# src/greenkube/core/optimization/analyzers/orphaned_pv.py
"""Cleanup recommendations for PersistentVolumes that appear unused.

The collector (``greenkube.collectors.pv_collector``) only reports volumes
whose claim is gone and that nothing else depends on, with the evidence; this
analyzer additionally requires the condition to have persisted for
``DELETION_MIN_UNUSED_DAYS`` and words the recommendation as a review.
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


class OrphanedPVAnalyzer(Analyzer):
    """Identifies released PersistentVolumes that have looked unused long enough, with evidence."""

    capability = "storage_cleanup"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        persistent_volumes = context.persistent_volumes
        if not persistent_volumes:
            return recs

        cfg = self.config
        now = ensure_utc(context.window_end or datetime.now(timezone.utc))
        minimum = min_unused_period(cfg)

        for pv in persistent_volumes:
            name = getattr(pv, "name", None)
            if not name:
                continue
            evidence = [str(item) for item in (getattr(pv, "evidence", None) or []) if item]
            first_seen = getattr(pv, "first_seen_at", None)
            # No evidence or no proven duration: nothing to recommend (fail closed).
            if not evidence or not isinstance(first_seen, datetime):
                continue
            unused_for = now - ensure_utc(first_seen)
            if unused_for < minimum:
                continue

            phase = getattr(pv, "phase", "") or ""
            capacity_bytes = getattr(pv, "capacity_bytes", 0) or 0
            reclaim_policy = getattr(pv, "reclaim_policy", "") or ""
            capacity_gib = capacity_bytes / (1024**3)

            # Deletion releases the asset's current allocated rate (realization 1).
            # CO2e is not projected: the energy model does not cover storage.
            annual_cost = getattr(pv, "annual_cost", None)
            if not isinstance(annual_cost, (int, float)) or annual_cost < 0:
                annual_cost = None
            currency = getattr(cfg, "IMPACT_PRICING_CURRENCY", None) or "USD"
            description_savings = ""
            cost_source = CostSource.OPENCOST
            cost_detail = "opencost_pv_cost"
            if annual_cost == 0:
                # OpenCost knows the volume at no cost: an observed zero, never the flat default (COST-03).
                description_savings = " OpenCost reports no cost for this volume: no cost saving."
            elif annual_cost is not None and capacity_bytes > 0:
                description_savings = f" OpenCost-reported cost savings: {annual_cost:.2f} {currency}/year."
            elif capacity_bytes > 0:
                annual_cost = capacity_gib * cfg.STORAGE_COST_PER_GIB_MONTH * 12
                cost_source = CostSource.CUSTOM_RATE
                cost_detail = "STORAGE_COST_PER_GIB_MONTH"
                description_savings = (
                    f" Estimated cost savings: {annual_cost:.2f} {currency}/year "
                    f"(at {cfg.STORAGE_COST_PER_GIB_MONTH:.2f} {currency}/GiB-month)."
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
                        storage_gib=capacity_gib,
                    )
                )

            since = ensure_utc(first_seen).strftime("%Y-%m-%d %H:%M UTC")
            evidence_text = "; ".join(evidence + [f"unused continuously since {since} ({format_days(unused_for)})"])
            recs.append(
                Recommendation(
                    pod_name=name,
                    type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
                    scope="cluster",
                    description=(
                        f"PersistentVolume '{name}' appears unused for {format_days(unused_for)} "
                        f"({capacity_gib:.1f}GiB, phase {phase or 'unknown'}, reclaim policy "
                        f"{reclaim_policy or 'unknown'}). Review its data (backups) before deleting: deleting it "
                        f"releases {capacity_gib:.1f}GiB of provisioned storage and cannot be undone."
                        f"{description_savings}"
                    ),
                    reason=(
                        f"Evidence that it is unused: {evidence_text}. Add the annotation "
                        f'greenkube.io/ignore="true" to the PersistentVolume to keep it.'
                    ),
                    priority="medium",
                    **savings_fields,
                )
            )
        return recs
