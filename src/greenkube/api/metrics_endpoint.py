# src/greenkube/api/metrics_endpoint.py
"""
Prometheus metrics exposition for GreenKube.

Exposes recommendation counts and potential savings as Prometheus Gauges
so they can be scraped by Prometheus and visualized in Grafana dashboards.
"""

import logging
from collections import defaultdict
from typing import List

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from greenkube.models.metrics import Recommendation

logger = logging.getLogger(__name__)

# Use a custom registry to avoid polluting the default registry with
# process/platform collectors that are irrelevant in a FastAPI context.
REGISTRY = CollectorRegistry()

RECOMMENDATION_COUNT = Gauge(
    "greenkube_recommendations_total",
    "Number of active recommendations by type and priority",
    ["type", "priority"],
    registry=REGISTRY,
)

RECOMMENDATION_SAVINGS_COST = Gauge(
    "greenkube_recommendations_savings_cost_dollars",
    "Total potential cost savings from recommendations by type",
    ["type"],
    registry=REGISTRY,
)

RECOMMENDATION_SAVINGS_CO2 = Gauge(
    "greenkube_recommendations_savings_co2e_grams",
    "Total potential CO2e savings from recommendations by type",
    ["type"],
    registry=REGISTRY,
)


def update_recommendation_metrics(recommendations: List[Recommendation]) -> None:
    """Updates Prometheus gauges with current recommendation counts and savings.

    Clears previous values and sets new ones based on the provided list.

    Args:
        recommendations: The current list of active recommendations.
    """
    # Reset all gauge label combinations
    RECOMMENDATION_COUNT._metrics.clear()
    RECOMMENDATION_SAVINGS_COST._metrics.clear()
    RECOMMENDATION_SAVINGS_CO2._metrics.clear()

    if not recommendations:
        return

    # Aggregate counts by (type, priority)
    count_by_type_priority: dict[tuple[str, str], int] = defaultdict(int)
    savings_cost_by_type: dict[str, float] = defaultdict(float)
    savings_co2_by_type: dict[str, float] = defaultdict(float)

    for rec in recommendations:
        rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)
        count_by_type_priority[(rec_type, rec.priority)] += 1
        savings_cost_by_type[rec_type] += rec.potential_savings_cost or 0.0
        savings_co2_by_type[rec_type] += rec.potential_savings_co2e_grams or 0.0

    for (rec_type, priority), count in count_by_type_priority.items():
        RECOMMENDATION_COUNT.labels(type=rec_type, priority=priority).set(count)

    for rec_type, cost in savings_cost_by_type.items():
        RECOMMENDATION_SAVINGS_COST.labels(type=rec_type).set(cost)

    for rec_type, co2 in savings_co2_by_type.items():
        RECOMMENDATION_SAVINGS_CO2.labels(type=rec_type).set(co2)

    logger.debug("Updated Prometheus metrics with %d recommendations.", len(recommendations))


def get_metrics_output() -> bytes:
    """Generates the Prometheus text exposition format output.

    Returns:
        Bytes containing the Prometheus text format metrics.
    """
    return generate_latest(REGISTRY)
