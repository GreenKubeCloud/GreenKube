# src/greenkube/core/optimization/analyzers/rightsizing_cpu.py
"""CPU rightsizing: targets whose CPU requests far exceed actual usage."""

import logging
from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import (
    balanced_rightsizing_target,
    latest_request_value,
    percentile,
    resource_savings_ratio,
    usage_stats,
)
from greenkube.core.optimization.targeting import (
    owner_fields,
    scope_for_target_kind,
    split_series_by_container,
    target_label,
)
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

LOG = logging.getLogger(__name__)


class CpuRightsizingAnalyzer(Analyzer):
    """Identifies targets with CPU requests much larger than actual usage."""

    capability = "cpu_rightsizing"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        projector = ImpactProjector.for_context(context)

        for (ns, target_kind, target_name), target_series in context.target_series().items():
            for container_name, series in split_series_by_container(target_series):
                cpu_request = latest_request_value(series, "cpu_request")
                if cpu_request == 0:
                    continue

                avg_usage, observed_max, usages = usage_stats(
                    series,
                    "cpu_usage_millicores",
                    "cpu_usage_max_millicores",
                )
                if not usages:
                    continue

                usage_ratio = avg_usage / cpu_request

                if usage_ratio < cfg.RIGHTSIZING_CPU_THRESHOLD:
                    p95 = percentile(usages, 95)
                    raw_recommended = balanced_rightsizing_target(cfg, avg_usage, observed_max, p95)
                    recommended = max(raw_recommended, cfg.RECOMMENDATION_MIN_CPU_MILLICORES)
                    if resource_savings_ratio(cpu_request, recommended) is None:
                        LOG.debug(
                            "Skipping CPU rightsizing for %s/%s: final recommendation %sm is not below current %sm.",
                            ns,
                            target_name,
                            recommended,
                            cpu_request,
                        )
                        continue

                    # The Impact Model prices only the released CPU reservation
                    # (BUG-15) and projects CO2e from the released idle and embodied share (BUG-16).
                    delta = projector.container_resources(
                        projector.workload_ref(ns, target_kind, target_name, container_name),
                        series,
                        cpu_millicores=(cpu_request, recommended),
                    )
                    label = target_label(target_kind, target_name)
                    if container_name:
                        label = f"{label} container '{container_name}'"
                    floor_description = ""
                    floor_reason = ""
                    if recommended != raw_recommended:
                        floor_description = f" (Floored to minimum: {cfg.RECOMMENDATION_MIN_CPU_MILLICORES}m CPU.)"
                        floor_reason = (
                            f" Recommended value was below the minimum of "
                            f"{cfg.RECOMMENDATION_MIN_CPU_MILLICORES}m; "
                            "floored to avoid impractically small requests."
                        )

                    recs.append(
                        Recommendation(
                            pod_name=target_name,
                            container_name=container_name,
                            namespace=ns,
                            type=RecommendationType.RIGHTSIZING_CPU,
                            scope=scope_for_target_kind(target_kind),
                            **owner_fields(target_kind, target_name),
                            description=(
                                f"{label} uses avg {avg_usage:.0f}m and max {observed_max:.0f}m of "
                                f"{cpu_request}m CPU "
                                f"requested ({usage_ratio:.0%}). Recommend reducing to {recommended}m."
                                f"{floor_description}"
                            ),
                            reason=(
                                f"Average CPU usage is {usage_ratio:.0%} of the request. "
                                f"P95 usage is {p95:.0f}m and observed max is {observed_max:.0f}m."
                                f"{floor_reason}"
                            ),
                            priority="medium",
                            current_cpu_request_millicores=cpu_request,
                            recommended_cpu_request_millicores=recommended,
                            **impact_fields(delta),
                        )
                    )
        return recs
