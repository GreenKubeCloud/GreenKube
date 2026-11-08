"""Ranking and decisioning of recommendations (roadmap I1-I3, F10).

``rank_recommendations`` ranks by projected savings (``co2`` or ``cost``) or by
the risk-adjusted expected value (``value``, or any ranking profile).
``decide_records`` attaches the live decision to persisted records: the
expected value under the requested profile and carbon price, with the source
track record, and the delivery eligibility.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from greenkube.models.metrics import Recommendation, RecommendationRecord, TopRecommendation

SavingsMetric = Literal["co2", "cost", "value"]


def normalize_savings_metric(metric: str | None) -> SavingsMetric:
    """Normalize a user-facing savings metric selector.

    Args:
        metric: Requested metric value. Supported canonical values are ``co2``, ``cost`` and
            ``value`` (risk-adjusted expected value).

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
    if normalized in {"value", "ev"}:
        return "value"
    raise ValueError("Unsupported recommendation savings metric. Use 'co2', 'cost' or 'value'.")


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


def _level_value(value) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def expected_value(recommendation: Any) -> float:
    """Risk-adjusted expected value (EV) per year, 0 when unscored."""
    value = getattr(recommendation, "value", None)
    if isinstance(value, dict) and isinstance(value.get("ev"), (int, float)):
        return float(value["ev"])
    return float(getattr(recommendation, "ranking_score", None) or 0.0)


def _sort_value(recommendation: Recommendation | RecommendationRecord, metric: SavingsMetric) -> float:
    if metric == "value":
        return expected_value(recommendation)
    return projected_co2e_grams(recommendation) if metric == "co2" else projected_cost(recommendation)


def _secondary_sort_value(recommendation: Recommendation | RecommendationRecord, metric: SavingsMetric) -> float:
    return projected_cost(recommendation) if metric == "co2" else projected_co2e_grams(recommendation)


def rank_recommendations(
    recommendations: Sequence[Recommendation | RecommendationRecord],
    limit: int | None = 5,
    savings_metric: str = "co2",
    profile: str | None = None,
    carbon_price: float | None = None,
    config: Any = None,
    track_record: Any = None,
) -> list[TopRecommendation]:
    """Rank recommendations by projected savings or by expected value.

    Args:
        recommendations: Active recommendations to rank.
        limit: Maximum number of ranked recommendations to return. ``None`` returns all ranked rows.
        savings_metric: ``co2`` (default) or ``cost`` rank recommendations with a positive
            projection by that saving; ``value`` ranks every recommendation by EV.
        profile: Optional ranking profile (``balanced``, ``carbon_first``, ``cost_first``,
            ``quick_wins``, ``low_risk``): recommendations are re-scored under it and ranked by EV.
        carbon_price: Optional carbon price override (currency per tonne) for the EV.
        config: Settings used to resolve the EV parameters.
        track_record: Source track record used by ``p_success``.

    Returns:
        Ranked top recommendations.
    """
    from greenkube.core.optimization.scoring import ranking_sort_key, score_recommendations

    metric = normalize_savings_metric(savings_metric)
    source = list(recommendations)

    if profile or carbon_price is not None:
        source = score_recommendations(
            source, profile=profile, carbon_price=carbon_price, config=config, track_record=track_record
        )

    if profile or metric == "value":
        # EV ranking: absolute, so the order never depends on the batch (BUG-23).
        ranked_source = sorted(source, key=ranking_sort_key)
    else:
        ranked_source = [rec for rec in source if _sort_value(rec, metric) > 0]
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
    sort_metric: SavingsMetric = "value" if profile else metric

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
            sort_metric=sort_metric,
            sort_value=_sort_value(rec, sort_metric),
            projected_savings_co2e_grams=projected_co2e_grams(rec),
            projected_savings_cost=projected_cost(rec),
            source=_level_value(getattr(rec, "source", None)) or "greenkube",
            risk_level=_level_value(getattr(rec, "risk_level", None)),
            confidence=getattr(rec, "confidence", None),
            effort=_level_value(getattr(rec, "effort", None)),
            ranking_score=getattr(rec, "ranking_score", None),
            ranking_factors=dict(getattr(rec, "ranking_factors", {}) or {}),
            expires_at=getattr(rec, "expires_at", None),
            value=getattr(rec, "value", None),
            eligibility=getattr(rec, "eligibility", None),
        )
        for index, rec in enumerate(ranked_source, start=1)
    ]


@dataclass(frozen=True)
class DecisionRequest:
    """Per-request decision parameters."""

    profile: Optional[str] = None
    carbon_price: Optional[float] = None


async def decide_records(
    records: Sequence[Any],
    *,
    config: Any,
    reco_repo: Any,
    gitops_repo: Any = None,
    pr_repo: Any = None,
    automation_configured: bool = False,
    request: DecisionRequest = DecisionRequest(),
    now: Optional[datetime] = None,
) -> List[Any]:
    """Attach the live expected value and delivery eligibility to persisted records.

    Records are re-scored with the measured source track record (I3) under the
    requested profile and carbon price (I2), using the delivery mode and the
    workload criticality known now, then sorted by EV. Reads only.
    """
    from greenkube.core.optimization import eligibility as elig
    from greenkube.core.optimization.scoring import resolve_parameters, score_recommendations, sort_by_value
    from greenkube.core.optimization.track_record import load_track_record

    if not records:
        return []
    current = now or datetime.now(timezone.utc)
    params = resolve_parameters(config, request.profile, request.carbon_price)
    book = await load_track_record(reco_repo)

    automation_enabled = bool(getattr(config, "AUTOMATION_ENABLED", False)) and (
        str(getattr(config, "AUTOMATION_MODE", "disabled")) != "disabled"
    )
    policy, policy_error = elig.load_policy(os.getenv("GITOPS_POLICY_JSON"))
    bindings = None
    open_prs: Optional[frozenset] = frozenset()
    if automation_configured and automation_enabled:
        # Bindings and pull requests only exist when automation runs; skip the reads otherwise.
        if gitops_repo is not None:
            cluster = getattr(config, "CLUSTER_NAME", None) or "default"
            bindings = await elig.load_bindings(gitops_repo, cluster, current)
        open_prs = await elig.load_open_pr_ids(pr_repo)
    from greenkube.utils.time import parse_query_step_seconds

    ctx = elig.EligibilityContext(
        now=current,
        automation_configured=automation_configured,
        automation_enabled=automation_enabled,
        policy=policy,
        policy_error=policy_error,
        bindings=bindings,
        open_pr_recommendation_ids=open_prs,
        step_seconds=parse_query_step_seconds(str(getattr(config, "PROMETHEUS_QUERY_RANGE_STEP", "") or "5m")),
    )
    policy_results = await elig.evaluate_policies(records, ctx, gitops_repo)
    if policy_results:
        ctx = replace(ctx, policy_results=policy_results)

    # Eligibility first (it decides the delivery mode), then value, then the final eligibility
    # that also sees the value lane.
    first = {getattr(record, "id", None): elig.assess_eligibility(record, ctx) for record in records}
    delivery = {
        rec_id: (elig.DELIVERY_DIRECT if decision["can_open_pr"] else elig.DELIVERY_MANUAL)
        for rec_id, decision in first.items()
        if rec_id is not None
    }
    scored = score_recommendations(
        records,
        params=params,
        track_record=book,
        delivery_modes=delivery,
        criticality=elig.criticality_by_record(records, ctx),
    )
    decided = [record.model_copy(update={"eligibility": elig.assess_eligibility(record, ctx)}) for record in scored]
    return sort_by_value(decided)
