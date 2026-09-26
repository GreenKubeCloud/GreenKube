# src/greenkube/core/optimization/analyzers/autoscaling.py
"""Autoscaling candidates: spiky CPU usage without an existing HPA."""

import logging
import math
from typing import TYPE_CHECKING, List

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.core.optimization.statistics import latest_request_value, usage_stats
from greenkube.core.optimization.targeting import owner_fields, scope_for_target_kind, target_label
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext

LOG = logging.getLogger(__name__)


class AutoscalingAnalyzer(Analyzer):
    """Identifies targets with spiky load patterns that would benefit from HPA.

    Skips pods whose owner (Deployment/StatefulSet) is already managed by
    an existing HorizontalPodAutoscaler.
    """

    capability = "autoscaling"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        hpa_targets = context.hpa_targets

        for (ns, target_kind, target_name), series in context.target_series().items():
            mean_usage, max_usage, usages = usage_stats(
                series,
                "cpu_usage_millicores",
                "cpu_usage_max_millicores",
            )
            if len(usages) < 3:
                continue

            cpu_request = latest_request_value(series, "cpu_request")
            if cpu_request == 0:
                continue

            if mean_usage == 0:
                continue

            variance = sum((u - mean_usage) ** 2 for u in usages) / len(usages)
            stddev = math.sqrt(variance)
            cv = stddev / mean_usage
            spike_ratio = max_usage / mean_usage

            if cv > cfg.AUTOSCALING_CV_THRESHOLD and spike_ratio > cfg.AUTOSCALING_SPIKE_RATIO:
                if hpa_targets and target_kind != "Pod" and (ns, target_kind, target_name) in hpa_targets:
                    LOG.debug(
                        "Skipping autoscaling recommendation for %s/%s: HPA already exists for %s/%s",
                        ns,
                        target_name,
                        target_kind,
                        target_name,
                    )
                    continue

                label = target_label(target_kind, target_name)

                recs.append(
                    Recommendation(
                        pod_name=target_name,
                        namespace=ns,
                        type=RecommendationType.AUTOSCALING_CANDIDATE,
                        scope=scope_for_target_kind(target_kind),
                        **owner_fields(target_kind, target_name),
                        description=(
                            f"{label} has highly variable CPU usage (CV={cv:.2f}, "
                            f"spike ratio={spike_ratio:.1f}x). Consider using HPA "
                            f"instead of static resource allocation."
                        ),
                        reason=(
                            f"CPU usage coefficient of variation is {cv:.2f} (threshold: "
                            f"{cfg.AUTOSCALING_CV_THRESHOLD}). Max/mean ratio is {spike_ratio:.1f}x."
                        ),
                        priority="medium",
                        current_cpu_request_millicores=cpu_request,
                    )
                )
        return recs
