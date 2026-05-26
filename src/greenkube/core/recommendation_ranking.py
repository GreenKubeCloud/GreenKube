"""Utilities for ranking actionable recommendations by projected savings."""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType, TopRecommendation

SavingsMetric = Literal["co2", "cost"]


def normalize_savings_metric(metric: str | None) -> SavingsMetric:
    """Normalize a user-facing savings metric selector.

    Args:
        metric: Requested metric value. Supported canonical values are ``co2`` and ``cost``.

    Returns:
        The canonical savings metric name.

    Raises:
        ValueError: If the metric is unsupported.
    """
    normalized = (metric or "co2").strip().lower()
    if normalized in {"co2", "co2e", "carbon"}:
        return "co2"
    if normalized in {"cost", "usd", "dollars"}:
        return "cost"
    raise ValueError("Unsupported recommendation savings metric. Use 'co2' or 'cost'.")


def recommendation_resource_label(recommendation: Recommendation | RecommendationRecord) -> str:
    """Return the display resource targeted by a recommendation."""
    target_node = getattr(recommendation, "target_node", None)
    if target_node:
        return target_node

    pod_name = getattr(recommendation, "pod_name", None)
    if pod_name:
        return pod_name

    namespace = getattr(recommendation, "namespace", None)
    if namespace:
        return namespace

    return "_cluster"


def recommendation_type_value(recommendation: Recommendation | RecommendationRecord) -> str:
    """Return the recommendation type as a stable string value."""
    rec_type = getattr(recommendation, "type")
    return rec_type.value if isinstance(rec_type, RecommendationType) else str(rec_type)


def projected_co2e_grams(recommendation: Recommendation | RecommendationRecord) -> float:
    """Return projected annual CO2e savings in grams."""
    return float(getattr(recommendation, "potential_savings_co2e_grams", None) or 0.0)


def projected_cost(recommendation: Recommendation | RecommendationRecord) -> float:
    """Return projected annual cloud cost savings."""
    return float(getattr(recommendation, "potential_savings_cost", None) or 0.0)


def _priority_weight(priority: str | None) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get((priority or "medium").lower(), 0)


def _timestamp_value(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.timestamp()


def _sort_value(recommendation: Recommendation | RecommendationRecord, metric: SavingsMetric) -> float:
    return projected_co2e_grams(recommendation) if metric == "co2" else projected_cost(recommendation)


def _secondary_sort_value(recommendation: Recommendation | RecommendationRecord, metric: SavingsMetric) -> float:
    return projected_cost(recommendation) if metric == "co2" else projected_co2e_grams(recommendation)


def rank_recommendations(
    recommendations: Sequence[Recommendation | RecommendationRecord],
    limit: int | None = 5,
    savings_metric: str = "co2",
) -> list[TopRecommendation]:
    """Rank recommendations by projected annual CO2e or cost savings.

    Args:
        recommendations: Active recommendations to rank.
        limit: Maximum number of ranked recommendations to return. ``None`` returns all ranked rows.
        savings_metric: Ranking metric, ``co2`` by default or ``cost``.

    Returns:
        Ranked top recommendations for the selected savings metric.
    """
    metric = normalize_savings_metric(savings_metric)
    ranked_source = [rec for rec in recommendations if _sort_value(rec, metric) > 0]
    ranked_source.sort(
        key=lambda rec: (
            _sort_value(rec, metric),
            _secondary_sort_value(rec, metric),
            _priority_weight(getattr(rec, "priority", None)),
            _timestamp_value(getattr(rec, "updated_at", None) or getattr(rec, "created_at", None)),
            int(getattr(rec, "id", None) or 0),
        ),
        reverse=True,
    )

    if limit is not None:
        ranked_source = ranked_source[: max(1, limit)]

    return [
        TopRecommendation(
            rank=index,
            id=getattr(rec, "id", None),
            type=getattr(rec, "type"),
            namespace=getattr(rec, "namespace", None),
            resource=recommendation_resource_label(rec),
            scope=getattr(rec, "scope", None) or "pod",
            priority=getattr(rec, "priority", None) or "medium",
            description=getattr(rec, "description"),
            reason=getattr(rec, "reason", "") or "",
            sort_metric=metric,
            sort_value=_sort_value(rec, metric),
            projected_savings_co2e_grams=projected_co2e_grams(rec),
            projected_savings_cost=projected_cost(rec),
        )
        for index, rec in enumerate(ranked_source, start=1)
    ]
