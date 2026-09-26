# src/greenkube/core/optimization/analyzers/carbon_aware.py
"""Carbon-aware scheduling: workloads running during high-intensity periods."""

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import annualized_window_total
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class CarbonAwareAnalyzer(Analyzer):
    """Identifies targets running during high carbon intensity periods."""

    capability = "carbon_aware"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config

        zone_intensities: Dict[str, List[float]] = defaultdict(list)
        for m in context.metrics:
            if m.emaps_zone and m.grid_intensity:
                zone_intensities[m.emaps_zone].append(m.grid_intensity)

        zone_avg: Dict[str, float] = {}
        for zone, vals in zone_intensities.items():
            zone_avg[zone] = sum(vals) / len(vals)

        for (ns, target_kind, target_name), series in context.target_series().items():
            agg: Dict = {"intensities": [], "zone": None, "co2e": 0.0}
            for metric in series:
                if metric.grid_intensity:
                    agg["intensities"].append(metric.grid_intensity)
                if metric.emaps_zone:
                    agg["zone"] = metric.emaps_zone
                agg["co2e"] += metric.co2e_grams

            zone = agg.get("zone")
            if not zone or zone not in zone_avg:
                continue
            intensities = agg["intensities"]
            if not intensities:
                continue

            pod_avg_intensity = sum(intensities) / len(intensities)
            zone_average = zone_avg[zone]

            if zone_average == 0:
                continue

            ratio = pod_avg_intensity / zone_average
            if ratio > cfg.CARBON_AWARE_THRESHOLD:
                label = target_label(target_kind, target_name)
                annual_co2e = annualized_window_total(agg["co2e"], context.analysis_window_seconds)
                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.CARBON_AWARE_SCHEDULING,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} runs during high carbon intensity periods "
                            f"(avg {pod_avg_intensity:.0f} vs zone avg {zone_average:.0f} gCO2e/kWh, "
                            f"{ratio:.1f}x). Consider scheduling during low-carbon windows."
                        ),
                        reason=(
                            f"Pod grid intensity is {ratio:.1f}x the zone average "
                            f"(threshold: {cfg.CARBON_AWARE_THRESHOLD}x)."
                        ),
                        priority="low",
                        potential_savings_co2e_grams=annual_co2e * (1 - 1 / ratio),
                    )
                )
        return recs
