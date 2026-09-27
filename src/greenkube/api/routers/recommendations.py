# src/greenkube/api/routers/recommendations.py
"""
API routes for the recommendation lifecycle.

Endpoints:
  GET  /recommendations            - Live recommendations (runs the optimization engine, upserts DB)
  GET  /recommendations/active     - Current active recommendations from DB
    GET  /recommendations/top        - Ranked actionable recommendations by projected savings
  GET  /recommendations/ignored    - All permanently ignored recommendations
  GET  /recommendations/history    - Historical records filtered by time range
  GET  /recommendations/savings    - Aggregate CO2 and cost savings from applied recs
  PATCH /recommendations/{id}/apply  - Mark a recommendation as applied
  PATCH /recommendations/{id}/ignore - Permanently ignore a recommendation
  DELETE /recommendations/{id}/ignore - Un-ignore a recommendation (restore to active)
"""

import logging
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
    get_savings_ledger_repository,
    validate_namespace,
)
from greenkube.api.metrics_endpoint import update_recommendation_metrics
from greenkube.core.config import get_config
from greenkube.core.optimization.engine import OptimizationEngine
from greenkube.core.recommendation_ranking import rank_recommendations
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    Recommendation,
    RecommendationEvent,
    RecommendationRecord,
    RecommendationSavingsSummary,
    TopRecommendation,
)
from greenkube.storage.base_repository import CombinedMetricsRepository, NodeRepository, RecommendationRepository
from greenkube.storage.base_savings_repository import SavingsLedgerRepository
from greenkube.utils.date_utils import parse_duration

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_optional_time_range(last: Optional[str]) -> tuple[Optional[datetime], Optional[datetime]]:
    """Compute an optional UTC time range from a dashboard window slug."""
    if not last:
        return None, None

    end = datetime.now(timezone.utc)
    if last.lower() == "ytd":
        return datetime(end.year, 1, 1, tzinfo=timezone.utc), end

    try:
        delta = parse_duration(last)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return end - delta, end


def _get_savings_cluster_name() -> str:
    """Return the cluster name used by the savings attribution task."""
    return get_config().CLUSTER_NAME or "default"


def _enum_value(value) -> Optional[str]:
    """Returns the string value of an enum or plain value."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _filter_records(
    records: List[RecommendationRecord],
    source: Optional[str],
    risk_level: Optional[str],
    capability: Optional[str],
) -> List[RecommendationRecord]:
    """Applies optional source/risk/capability filters in Python."""
    result = records
    if source:
        result = [r for r in result if _enum_value(r.source) == source]
    if risk_level:
        result = [r for r in result if _enum_value(r.risk_level) == risk_level]
    if capability:
        result = [r for r in result if _enum_value(r.capability) == capability]
    return result


def _summary_from_savings_totals(
    totals_by_type: dict[str, dict[str, float]],
    applied_count: int,
    namespace: Optional[str],
    totals_by_method: Optional[dict[str, dict[str, float]]] = None,
) -> RecommendationSavingsSummary:
    """Build an API savings summary from ledger totals grouped by recommendation type."""
    total_carbon = sum(values.get("co2e_saved_grams", 0.0) for values in totals_by_type.values())
    total_cost = sum(values.get("cost_saved_dollars", 0.0) for values in totals_by_type.values())
    namespace_breakdown = []
    if namespace is not None:
        namespace_breakdown.append(
            {
                "namespace": namespace,
                "carbon_saved_co2e_grams": total_carbon,
                "cost_saved": total_cost,
                "count": applied_count,
            }
        )

    measured = (totals_by_method or {}).get("measured", {})
    prorated = (totals_by_method or {}).get("prorated", {})
    measured_carbon = measured.get("co2e_saved_grams", 0.0)
    measured_cost = measured.get("cost_saved_dollars", 0.0)
    if totals_by_method:
        prorated_carbon = prorated.get("co2e_saved_grams", 0.0)
        prorated_cost = prorated.get("cost_saved_dollars", 0.0)
    else:
        prorated_carbon, prorated_cost = total_carbon, total_cost

    return RecommendationSavingsSummary(
        total_carbon_saved_co2e_grams=total_carbon,
        total_cost_saved=total_cost,
        applied_count=applied_count,
        namespace_breakdown=namespace_breakdown,
        measured_carbon_saved_co2e_grams=measured_carbon,
        measured_cost_saved=measured_cost,
        prorated_carbon_saved_co2e_grams=prorated_carbon,
        prorated_cost_saved=prorated_cost,
    )


async def _generate_and_persist_recommendations(
    namespace: Optional[str],
    repo: CombinedMetricsRepository,
    node_repo: NodeRepository,
    reco_repo: RecommendationRepository,
) -> list[Recommendation]:
    """Run the optimization engine and reconcile persisted active recommendations."""
    engine = OptimizationEngine()
    recommendations = await engine.refresh(repo, node_repo, reco_repo, namespace=namespace)
    update_recommendation_metrics(recommendations)
    return recommendations


@router.get("/recommendations", response_model=List[Recommendation])
async def list_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Analyze recent metrics, upsert recommendations in DB, and return them.

    Recommended CPU and memory values are guaranteed to be at least the
    configured minimums (RECOMMENDATION_MIN_CPU_MILLICORES /
    RECOMMENDATION_MIN_MEMORY_BYTES), so all returned recommendations are
    actionable as-is.
    """
    return await _generate_and_persist_recommendations(namespace, repo, node_repo, reco_repo)


@router.get("/recommendations/active", response_model=List[RecommendationRecord])
async def list_active_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    refresh: bool = Query(False, description="Refresh recommendations before returning active records."),
    source: Optional[str] = Query(None, description="Filter by recommendation source (greenkube, vpa, karpenter)."),
    risk_level: Optional[str] = Query(None, description="Filter by risk level (low, medium, high)."),
    capability: Optional[str] = Query(None, description="Filter by optimization capability."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return currently active recommendations from the database.

    By default this endpoint reads directly from the DB. Pass ``refresh=true``
    to run the optimizer first and reconcile stale active records.
    """
    if refresh:
        try:
            await _generate_and_persist_recommendations(namespace, repo, node_repo, reco_repo)
        except Exception as e:
            logger.error("Failed to refresh active recommendations: %s", e)

    records = await reco_repo.get_active_recommendations(namespace=namespace)
    return _filter_records(records, source, risk_level, capability)


@router.get("/recommendations/top", response_model=List[TopRecommendation])
async def list_top_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    limit: int = Query(5, ge=1, le=50, description="Number of recommendations to return."),
    metric: Literal["co2", "cost"] = Query("co2", description="Savings metric used for ranking."),
    profile: Optional[str] = Query(
        None,
        description=(
            "Multi-criteria ranking profile: balanced, carbon_first, cost_first, quick_wins, low_risk. "
            "When omitted, ranking uses projected savings only."
        ),
    ),
    refresh: bool = Query(False, description="Refresh recommendations before ranking active records."),
    repo: CombinedMetricsRepository = Depends(get_combined_metrics_repository),
    node_repo: NodeRepository = Depends(get_node_repository),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return the highest-impact active recommendations.

    The default ranking uses projected CO2e savings for the coming year. Pass
    ``metric=cost`` to prioritize direct cloud cost savings, or ``profile`` to
    rank by the multi-criteria score (impact, confidence, risk, effort).
    """
    if refresh:
        try:
            await _generate_and_persist_recommendations(namespace, repo, node_repo, reco_repo)
        except Exception as e:
            logger.error("Failed to refresh top recommendations: %s", e)

    if profile:
        records = await reco_repo.get_active_recommendations(namespace=namespace)
    else:
        records = await reco_repo.get_top_recommendations(limit=limit, savings_metric=metric, namespace=namespace)
    return rank_recommendations(records, limit=limit, savings_metric=metric, profile=profile)


@router.get("/recommendations/ignored", response_model=List[RecommendationRecord])
async def list_ignored_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return all permanently ignored recommendations.

    Useful for reviewing ignored recommendations and deciding to un-ignore them.
    """
    return await reco_repo.get_ignored_recommendations(namespace=namespace)


@router.get("/recommendations/history", response_model=List[RecommendationRecord])
async def list_recommendation_history(
    start: str = Query(..., description="Start datetime (ISO 8601)."),
    end: str = Query(..., description="End datetime (ISO 8601)."),
    type: Optional[str] = Query(None, description="Filter by recommendation type."),
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Retrieve all recommendation records within a time range (any status)."""
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

    return await reco_repo.get_recommendations(
        start=start_dt,
        end=end_dt,
        rec_type=type,
        namespace=namespace,
    )


@router.get("/recommendations/applied", response_model=List[RecommendationRecord])
async def list_applied_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return all applied recommendations, ordered by most recently applied.

    Used by the Realized Savings section to show the details of each
    implemented optimization.
    """
    return await reco_repo.get_applied_recommendations(namespace=namespace)


@router.get("/recommendations/savings", response_model=RecommendationSavingsSummary)
async def get_savings_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '24h', '7d', '30d', 'ytd')."),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    savings_repo: SavingsLedgerRepository = Depends(get_savings_ledger_repository),
):
    """Return aggregate CO2e and cost savings from all applied recommendations.

    This is GreenKube's core value metric: the total environmental and financial
    impact of the optimizations that have been implemented.
    """
    start, end = _get_optional_time_range(last)
    record_summary = await reco_repo.get_savings_summary(namespace=namespace, start=start, end=end)

    if start is None or end is None:
        return record_summary

    try:
        cluster_name = _get_savings_cluster_name()
        totals = await savings_repo.get_window_totals(
            cluster_name=cluster_name,
            start_time=start,
            end_time=end,
            namespace=namespace,
        )

        if totals:
            by_method = await savings_repo.get_window_totals(
                cluster_name=cluster_name,
                start_time=start,
                end_time=end,
                namespace=namespace,
                group_by_method=True,
            )
            return _summary_from_savings_totals(totals, record_summary.applied_count, namespace, by_method)
    except Exception as exc:
        logger.warning("Could not load savings ledger summary: %s. Falling back to recommendation records.", exc)

    return record_summary


@router.get("/recommendations/{rec_id}", response_model=RecommendationRecord)
async def get_recommendation_detail(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return a single recommendation with its full evidence block and assessment.

    This is the review endpoint: it returns the observation window, utilization
    distribution, proposed change, confidence, risk factors, rollback conditions
    and expiry needed to decide without re-running the analysis.
    """
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    return record


@router.patch("/recommendations/{rec_id}/apply", response_model=RecommendationRecord)
async def apply_recommendation(
    rec_id: int,
    request: ApplyRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Mark a recommendation as applied.

    Optionally provide the actual values applied (CPU/memory). If actual savings
    are not provided, the estimated potential savings are used as a conservative proxy.

    A recommendation is considered applied even when the actual value deviates from
    the recommendation (e.g., reducing CPU to 50m instead of the suggested 40m).
    Applying freezes the verification baseline and starts the observation window.
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle

    lifecycle = RecommendationLifecycle(reco_repo)
    try:
        return await lifecycle.apply(
            rec_id,
            actual_cpu=request.actual_cpu_request_millicores,
            actual_memory=request.actual_memory_request_bytes,
            carbon_saved_co2e_grams=request.carbon_saved_co2e_grams,
            cost_saved=request.cost_saved,
            application_method=request.application_method or "manual",
            actor="user",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/recommendations/{rec_id}/events", response_model=List[RecommendationEvent])
async def list_recommendation_events(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Return the full lifecycle audit trail for a recommendation.

    Every transition (created, applied, pr_opened, verified, rollback_review,
    reverted, expired, ...) is recorded with actor, payload and timestamp.
    """
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    return await reco_repo.get_events(rec_id)


@router.patch("/recommendations/{rec_id}/ignore", response_model=RecommendationRecord)
async def ignore_recommendation(
    rec_id: int,
    request: IgnoreRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Permanently ignore a recommendation.

    The recommendation will no longer appear in the active list. It remains visible
    under GET /recommendations/ignored so it can be reviewed or un-ignored later.

    Typical use case: a pod cannot support HPA due to a RWO PVC, or a namespace
    intentionally runs at low utilization (e.g., a staging environment).
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle

    try:
        record = await reco_repo.ignore_recommendation(rec_id, request)
        await RecommendationLifecycle(reco_repo).record_event(
            rec_id, "ignored", actor="user", payload={"reason": request.reason}
        )
        return record
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/recommendations/{rec_id}/ignore", response_model=RecommendationRecord)
async def unignore_recommendation(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Restore an ignored recommendation back to active status.

    Useful when circumstances change (e.g., the PVC is migrated to RWX) or when a
    recommendation was accidentally ignored.
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle

    try:
        record = await reco_repo.unignore_recommendation(rec_id)
        await RecommendationLifecycle(reco_repo).record_event(rec_id, "unignored", actor="user")
        return record
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
