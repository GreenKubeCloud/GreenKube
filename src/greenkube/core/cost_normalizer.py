# src/greenkube/core/cost_normalizer.py
"""Normalizes OpenCost data per-step or per-range."""

import logging
from typing import Dict

from ..core.config import config
from ..models.metrics import CostMetric

logger = logging.getLogger(__name__)


class CostNormalizer:
    """Divides OpenCost daily / range totals into per-step values."""

    @staticmethod
    def per_step_cost(
        cost_map: Dict[str, CostMetric],
        pod_name: str,
        steps_per_day: float,
    ) -> float:
        """Return normalised per-step cost for a pod.

        Args:
            cost_map: Map of pod_name -> CostMetric.
            pod_name: The pod to look up.
            steps_per_day: Number of steps in 24 h (86400 / step_sec).

        Returns:
            Normalised cost or ``config.DEFAULT_COST`` when data is missing.
        """
        cost_metric = cost_map.get(pod_name)
        if cost_metric:
            return cost_metric.total_cost / steps_per_day
        return config.DEFAULT_COST

    @staticmethod
    def per_range_cost(
        cost_map: Dict[str, CostMetric],
        pod_name: str,
        steps_in_range: float,
    ) -> float:
        """Return normalised per-step cost for a pod over a range.

        Args:
            cost_map: Map of pod_name -> CostMetric.
            pod_name: The pod to look up.
            steps_in_range: Total number of steps in the requested range.

        Returns:
            Normalised cost or ``config.DEFAULT_COST`` when data is missing.
        """
        cost_metric = cost_map.get(pod_name)
        if cost_metric:
            return cost_metric.total_cost / steps_in_range
        return config.DEFAULT_COST
