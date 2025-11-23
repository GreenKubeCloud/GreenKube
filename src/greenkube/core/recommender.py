# src/greenkube/core/recommender.py

import logging
from typing import List

from greenkube.core.config import config
from greenkube.data.instance_profiles import INSTANCE_PROFILES
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

LOG = logging.getLogger(__name__)


class Recommender:
    """
    Analyzes combined metrics to generate optimization recommendations.
    """

    def __init__(
        self,
        rightsizing_threshold: float = 0.2,
        zombie_cost_threshold: float = 0.01,
        zombie_energy_threshold: float = 1000,
    ):
        """
        Initializes the recommender with specific thresholds.

        :param rightsizing_threshold: CPU usage percentage (0.0 to 1.0)
                                      below which a pod is considered oversized.
        :param zombie_cost_threshold: Minimum total cost for a pod to be
                                      considered a potential zombie.
        :param zombie_energy_threshold: Maximum energy (in Joules) for a pod
                                        to be considered a potential zombie.
        """
        self.rightsizing_threshold = rightsizing_threshold
        self.zombie_cost_threshold = zombie_cost_threshold
        self.zombie_energy_threshold = zombie_energy_threshold
        LOG.debug(
            "Recommender initialized with thresholds: Rightsizing=%s ZombieCost=%s ZombieEnergy=%s",
            self.rightsizing_threshold,
            self.zombie_cost_threshold,
            self.zombie_energy_threshold,
        )

    def _estimate_cpu_usage_percent(self, metric: CombinedMetric, all_metrics: List[CombinedMetric]) -> float:
        """
        Estimates the CPU usage percentage relative to its request,
        based on energy consumption (Joules) and instance profiles.

        Formula: Approximate CPU Usage = (Current Watts - Min Watts) / (Max Watts - Min Watts)
        """
        if metric.cpu_request == 0:
            return 0.0

        # Calculate Current Watts
        duration = metric.duration_seconds or 1  # Avoid division by zero
        current_watts = metric.joules / duration

        # Fallback to default profile values
        min_watts = config.DEFAULT_INSTANCE_MIN_WATTS
        max_watts = config.DEFAULT_INSTANCE_MAX_WATTS
        vcores = config.DEFAULT_INSTANCE_VCORES

        # Lookup specific profile if available
        if metric.node_instance_type:
            profile = INSTANCE_PROFILES.get(metric.node_instance_type)
            if profile:
                min_watts = profile.get("minWatts", min_watts)
                max_watts = profile.get("maxWatts", max_watts)
                vcores = profile.get("vcores", vcores)

        # Calculating utilization
        # Power = Min + Util * (Max - Min)
        # Util = (Power - Min) / (Max - Min)

        if max_watts == min_watts:
            return 0.0

        cpu_util_fraction = (current_watts - min_watts) / (max_watts - min_watts)
        cpu_util_fraction = max(0.0, min(cpu_util_fraction, 1.0))

        implied_cores = cpu_util_fraction * vcores

        # Compare against request (in millicores, so divide by 1000 for cores)
        request_cores = metric.cpu_request / 1000.0

        if request_cores == 0:
            return 0.0

        usage_percent = implied_cores / request_cores
        return usage_percent

    def generate_rightsizing_recommendations(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Identifies pods that are significantly underutilized (oversized)."""
        recommendations: List[Recommendation] = []

        if not metrics:
            return []

        # Pre-calculate usage for all metrics once
        usage_map = {m.pod_name: self._estimate_cpu_usage_percent(m, metrics) for m in metrics}

        for metric in metrics:
            # We can only rightsize pods that have a CPU request
            if metric.cpu_request == 0:
                continue

            usage_percent = usage_map.get(metric.pod_name, 0.0)

            if usage_percent < self.rightsizing_threshold and usage_percent > 0:
                desc = (
                    f"Pod is only using {usage_percent:.1%} of its requested "
                    f"{metric.cpu_request}m CPU (based on energy consumption)."
                )
                recommendations.append(
                    Recommendation(
                        pod_name=metric.pod_name,
                        namespace=metric.namespace,
                        type=RecommendationType.RIGHTSIZING_CPU,
                        description=desc,
                    )
                )
                LOG.debug(
                    "Generated RIGHTSIZING_CPU recommendation for %s/%s",
                    metric.namespace,
                    metric.pod_name,
                )

        return recommendations

    def generate_zombie_recommendations(self, metrics: List[CombinedMetric]) -> List[Recommendation]:
        """Identifies pods with cost but near-zero energy consumption."""
        recommendations: List[Recommendation] = []

        for metric in metrics:
            # Check for cost above threshold AND energy below threshold
            if metric.total_cost > self.zombie_cost_threshold and metric.joules < self.zombie_energy_threshold:
                desc = (
                    f"Pod cost {metric.total_cost:.4f} but "
                    f"consumed only {metric.joules} Joules. "
                    f"This may be an idle or 'zombie' pod."
                )
                recommendations.append(
                    Recommendation(
                        pod_name=metric.pod_name,
                        namespace=metric.namespace,
                        type=RecommendationType.ZOMBIE_POD,
                        description=desc,
                    )
                )
                LOG.debug(f"Generated ZOMBIE_POD recommendation for {metric.namespace}/{metric.pod_name}")

        return recommendations
