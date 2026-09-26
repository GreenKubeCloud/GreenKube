# src/greenkube/core/optimization/analyzers/rightsizing_memory.py
"""Memory rightsizing: targets whose memory requests far exceed actual usage."""

import logging
from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import (
    annualized_window_total,
    balanced_rightsizing_target,
    latest_request_value,
    percentile,
    resource_savings_ratio,
    usage_stats,
)
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

LOG = logging.getLogger(__name__)


class MemoryRightsizingAnalyzer(Analyzer):
    """Identifies targets with memory requests much larger than actual usage."""

    capability = "memory_rightsizing"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config

        for (ns, target_kind, target_name), series in context.target_series().items():
            mem_request = latest_request_value(series, "memory_request")
            if mem_request == 0:
                continue

            avg_usage, observed_max, usages = usage_stats(
                series,
                "memory_usage_bytes",
                "memory_usage_max_bytes",
            )
            if not usages:
                continue

            usage_ratio = avg_usage / mem_request

            if usage_ratio < cfg.RIGHTSIZING_MEMORY_THRESHOLD:
                p95 = percentile(usages, 95)
                raw_recommended = balanced_rightsizing_target(cfg, avg_usage, observed_max, p95)
                recommended = max(raw_recommended, cfg.RECOMMENDATION_MIN_MEMORY_BYTES)
                savings_ratio = resource_savings_ratio(mem_request, recommended)
                if savings_ratio is None:
                    LOG.debug(
                        "Skipping memory rightsizing for %s/%s: final %s bytes is not below current %s bytes.",
                        ns,
                        target_name,
                        recommended,
                        mem_request,
                    )
                    continue

                total_cost = sum(m.total_cost for m in series)
                total_co2 = sum(m.co2e_grams for m in series)
                annual_cost = annualized_window_total(total_cost, context.analysis_window_seconds)
                annual_co2 = annualized_window_total(total_co2, context.analysis_window_seconds)
                label = target_label(target_kind, target_name)
                floor_description = ""
                floor_reason = ""
                if recommended != raw_recommended:
                    min_memory_mib = cfg.RECOMMENDATION_MIN_MEMORY_BYTES // (1024 * 1024)
                    floor_description = f" (Floored to minimum: {min_memory_mib}MiB memory.)"
                    floor_reason = (
                        f" Recommended memory was below the minimum of {min_memory_mib}MiB; "
                        "floored to avoid impractically small requests."
                    )

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.RIGHTSIZING_MEMORY,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} uses avg {avg_usage / (1024 * 1024):.0f}MiB and max "
                            f"{observed_max / (1024 * 1024):.0f}MiB of "
                            f"{mem_request / (1024 * 1024):.0f}MiB memory requested ({usage_ratio:.0%}). "
                            f"Recommend reducing to {recommended / (1024 * 1024):.0f}MiB."
                            f"{floor_description}"
                        ),
                        reason=(
                            f"Average memory usage is {usage_ratio:.0%} of the request. "
                            f"P95 usage is {p95 / (1024 * 1024):.0f}MiB and observed max is "
                            f"{observed_max / (1024 * 1024):.0f}MiB."
                            f"{floor_reason}"
                        ),
                        priority="medium",
                        current_memory_request_bytes=mem_request,
                        recommended_memory_request_bytes=recommended,
                        potential_savings_cost=annual_cost * savings_ratio,
                        potential_savings_co2e_grams=annual_co2 * savings_ratio,
                    )
                )
        return recs
