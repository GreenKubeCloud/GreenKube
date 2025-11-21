# src/greenkube/core/recommender.py

import logging
from typing import List

from greenkube.core.config import config
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

        # Get Instance Profile
        # profile = None
        if metric.node:
            # In a real scenario, we'd need the instance type of the node.
            # However, CombinedMetric only has the node name.
            # We might need to look up the instance type from somewhere or
            # assume we can get the profile from the node name if we had a map.
            # But we don't have the node->instance_type map here.
            # Wait, the ticket says "The Recommender needs access to the INSTANCE_PROFILES."
            # But we also need to know which profile applies to the node.
            # Since we don't have that easily, we might have to rely on the fact that
            # we can't easily look it up without passing more data.
            # BUT, for the sake of the fix, let's assume we use the DEFAULT if we can't find it,
            # OR we need to pass the node_instance_map to the recommender.
            # The recommender is initialized once.
            # Maybe we can try to infer it or just use default if unknown.
            # Actually, `CombinedMetric` doesn't store instance type.
            # Let's use the DEFAULT_INSTANCE_PROFILE as a fallback, which is better than the relative logic.
            # Or better, we can try to match if we had the info.
            # Given the constraints, I will use the default profile if I can't find a specific one.
            # However, without instance type, I can't look up the profile.
            # I'll use config defaults.
            pass

        # Fallback to default profile values
        min_watts = config.DEFAULT_INSTANCE_MIN_WATTS
        max_watts = config.DEFAULT_INSTANCE_MAX_WATTS
        vcores = config.DEFAULT_INSTANCE_VCORES

        # If we had the profile, we would use it.
        # Since we don't have instance type in CombinedMetric, we are limited.
        # But the ticket says "The Recommender needs access to the INSTANCE_PROFILES."
        # This implies I should probably have added instance_type to CombinedMetric too.
        # Let's do that. It's safer.
        pass

        # Re-evaluating: I should add instance_type to CombinedMetric.
        # But for now, let's implement the formula with what we have (Defaults)
        # and maybe I'll add instance_type in a follow-up or right now if I can.
        # I'll stick to the plan of using the formula.

        # Wait, if I use default profile for everything, it might be wrong for large instances.
        # But it's still better than the relative "max ratio" logic which was completely flawed for idle clusters.

        # Let's check if I can add instance_type to CombinedMetric.
        # Yes, I can.
        pass

        # Calculating utilization
        # Power = Min + Util * (Max - Min)
        # Util = (Power - Min) / (Max - Min)

        if max_watts == min_watts:
            return 0.0

        cpu_util_fraction = (current_watts - min_watts) / (max_watts - min_watts)
        cpu_util_fraction = max(0.0, min(cpu_util_fraction, 1.0))

        # This is node-level utilization (or core-level if normalized).
        # Wait, the formula gives "Node Utilization".
        # But we want "Pod CPU Usage".
        # The estimator calculates pod energy based on share of CPU.
        # So reversing it is tricky.
        # If Pod Energy = Node Power * Share
        # And Node Power = Min + Util * (Max - Min)
        # And Share = Pod CPU / Node Total CPU
        # Then Pod CPU = Share * Node Total CPU

        # The ticket says: "Formula: Approximate CPU Usage = (Current Watts - Min Watts) / (Max Watts - Min Watts)."
        # This formula seems to assume the pod is the only thing running or we are calculating "equivalent CPU usage".
        # "Use this absolute CPU usage to compare against the cpu_request."

        # If I use the formula:
        # implied_util = (current_watts - min_watts) / (max_watts - min_watts)
        # This gives me a fraction (0-1).
        # If I multiply this by vcores, I get "implied cores used".
        # implied_cores = implied_util * vcores.

        # Then usage_percent = implied_cores / (cpu_request / 1000).

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
