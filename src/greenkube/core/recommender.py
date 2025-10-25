# src/greenkube/core/recommender.py

import logging
from typing import List
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
        LOG.debug(f"Recommender initialized with thresholds: Rightsizing({rightsizing_threshold}), ZombieCost({zombie_cost_threshold}), ZombieEnergy({zombie_energy_threshold})")

    def _estimate_cpu_usage_percent(
        self,
        metric: CombinedMetric,
        all_metrics: List[CombinedMetric]
    ) -> float:
        """
        Estimates the CPU usage percentage relative to its request,
        based on energy consumption (Joules).
        
        This logic assumes that within the cluster, a pod with the highest
        Joules-per-requested-millicore ratio is "fully utilized".
        All other pods are scaled relative to this benchmark.
        
        NOTE: This is an estimation. A more accurate approach would be to
        correlate Joules with CPU seconds from Kepler if available.
        """
        if metric.cpu_request == 0:
            return 0.0 # Cannot calculate usage % without a request

        # Find the max Joules-per-millicore ratio in the dataset
        # This acts as our "100% usage" benchmark
        max_ratio = 0.0
        for m in all_metrics:
            if m.cpu_request > 0:
                ratio = m.joules / m.cpu_request
                if ratio > max_ratio:
                    max_ratio = ratio
        
        if max_ratio == 0:
            return 0.0 # No pod in the dataset has any usage or requests

        # Calculate this pod's ratio
        current_ratio = metric.joules / metric.cpu_request
        
        # Return the usage percent relative to the max
        # Cap at 1.0 (100%) in case of minor fluctuations
        usage_percent = min(current_ratio / max_ratio, 1.0)
        return usage_percent

    def generate_rightsizing_recommendations(
        self, metrics: List[CombinedMetric]
    ) -> List[Recommendation]:
        """Identifies pods that are significantly underutilized (oversized)."""
        recommendations: List[Recommendation] = []
        
        if not metrics:
            return []
            
        # Pre-calculate usage for all metrics once
        usage_map = {
            m.pod_name: self._estimate_cpu_usage_percent(m, metrics)
            for m in metrics
        }
        
        for metric in metrics:
            # We can only rightsize pods that have a CPU request
            if metric.cpu_request == 0:
                continue
            
            usage_percent = usage_map.get(metric.pod_name, 0.0)
            
            if usage_percent < self.rightsizing_threshold and usage_percent > 0:
                desc = (
                    f"Pod is only using {usage_percent:.1%} of its requested "
                    f"{metric.cpu_request}m CPU (based on energy consumption). "
                    f"Consider lowering the request."
                )
                recommendations.append(
                    Recommendation(
                        pod_name=metric.pod_name,
                        namespace=metric.namespace,
                        type=RecommendationType.RIGHTSIZING_CPU,
                        description=desc,
                    )
                )
                LOG.debug(f"Generated RIGHTSIZING_CPU recommendation for {metric.namespace}/{metric.pod_name}")

        return recommendations

    def generate_zombie_recommendations(
        self, metrics: List[CombinedMetric]
    ) -> List[Recommendation]:
        """Identifies pods with cost but near-zero energy consumption."""
        recommendations: List[Recommendation] = []
        
        for metric in metrics:
            # Check for cost above threshold AND energy below threshold
            if (
                metric.total_cost > self.zombie_cost_threshold
                and metric.joules < self.zombie_energy_threshold
            ):
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

