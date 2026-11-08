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

        # Zone average weighted by samples, over every pod of the zone.
        zone_totals: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])
        summaries = list(context.workload_summaries())
        for _target, summary in summaries:
            if summary.emaps_zone and summary.grid_intensity and summary.intensity_sample_count:
                totals = zone_totals[summary.emaps_zone]
                totals[0] += summary.grid_intensity * summary.intensity_sample_count
                totals[1] += summary.intensity_sample_count
        zone_avg = {zone: total / count for zone, (total, count) in zone_totals.items() if count}

        for (ns, target_kind, target_name), summary in summaries:
            zone = summary.emaps_zone
            if not zone or zone not in zone_avg or not summary.grid_intensity:
                continue

            pod_avg_intensity = summary.grid_intensity
            zone_average = zone_avg[zone]

            if zone_average == 0:
                continue

            ratio = pod_avg_intensity / zone_average
            if ratio > cfg.CARBON_AWARE_THRESHOLD:
                label = target_label(target_kind, target_name)
                annual_co2e = annualized_window_total(summary.co2e_grams, context.analysis_window_seconds)
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
