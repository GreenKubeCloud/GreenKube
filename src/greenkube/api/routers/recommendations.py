# src/greenkube/api/routers/recommendations.py
"""
API routes for the recommendation lifecycle.

Endpoints:
  GET  /recommendations            - Current active recommendations from DB
  GET  /recommendations/active     - Current active recommendations from DB (filterable)
  GET  /recommendations/top        - Ranked recommendations by projected savings or expected value
  GET  /recommendations/ignored    - All permanently ignored recommendations
  GET  /recommendations/history    - Historical records filtered by time range
  GET  /recommendations/savings    - Aggregate CO2 and cost savings from applied recs
  GET  /recommendations/export     - Active recommendations as CSV or JSON, with released capacity
  PATCH /recommendations/{id}/apply  - Mark a recommendation as applied
  POST /recommendations/{id}/remeasure - Start a new measurement of a not-realized/inconclusive change
  POST /recommendations/{id}/rollback-review/dismiss - Dismiss a false-positive rollback review
  PATCH /recommendations/{id}/ignore - Permanently ignore a recommendation
  DELETE /recommendations/{id}/ignore - Un-ignore a recommendation (restore to active)
  GET  /sources/track-record       - Measured realization rate per source family and capability

Active lists, the top list and the detail carry the live decision: the
risk-adjusted expected value (``value``, ``ranking_score``) under the requested
``profile`` and ``carbon_price``, and the delivery ``eligibility`` (F10, I1-I3).

GET endpoints never run the optimizer and never write recommendations. The
deprecated ``refresh=true`` parameter only queues an optimization run for the
controller and returns its id in the ``X-GreenKube-Run-Id`` header; use
``POST /api/v1/optimization/runs`` instead.
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import List, Literal, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_authenticated_actor,
    get_automation_plan_service,
    get_gitops_binding_repository,
    get_pull_request_repository,
    get_recommendation_repository,
    get_savings_ledger_repository,
    require_scope,
    validate_namespace,
)
from greenkube.api.routers.optimization import enqueue_refresh, get_optimization_run_repository
from greenkube.auth.principal import Scope
from greenkube.core.config import get_config
from greenkube.core.optimization.scoring import normalize_profile, profile_names
from greenkube.core.optimization.track_record import load_track_record
from greenkube.core.recommendation_ranking import DecisionRequest, decide_records, rank_recommendations
from greenkube.core.savings_attributor import record_user_declared_savings, validate_user_declared_savings
from greenkube.core.savings_report import assemble_savings_report, interim_recommendation_ids
from greenkube.models.metrics import (
    ApplyRecommendationRequest,
    IgnoreRecommendationRequest,
    RecommendationEvent,
    RecommendationRecord,
    TopRecommendation,
)
from greenkube.models.savings import SavingsReport
from greenkube.models.verification import DismissRollbackReviewRequest, RemeasureRequest
from greenkube.storage.base_gitops_repository import GitOpsRepository
from greenkube.storage.base_optimization_run_repository import OptimizationRunRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.storage.base_savings_repository import SavingsLedgerRepository
from greenkube.storage.ledger_repository import LedgerRepository, get_ledger_repository
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
        # Every source behind a consensus recommendation matches (E6/G1), as in /api/v2/recommendations.
        result = [
            r
            for r in result
            if _enum_value(r.source) == source
            or source in r.sources
            or source in ((r.consensus or {}).get("source_ids") or [] if isinstance(r.consensus, dict) else [])
        ]
    if risk_level:
        result = [r for r in result if _enum_value(r.risk_level) == risk_level]
    if capability:
        result = [r for r in result if _enum_value(r.capability) == capability]
    return result


_REFRESH_DESCRIPTION = (
    "Deprecated: queue an optimization run for the controller (see POST /optimization/runs) and return "
    "its id in the X-GreenKube-Run-Id header. The response always contains the current data."
)

_PROFILE_DESCRIPTION = (
    "Ranking profile, a preset over the carbon price, risk aversion and effort weight of the expected value: "
    + ", ".join(profile_names())
    + ". Defaults to RECOMMENDATION_RANKING_PROFILE."
)
_CARBON_PRICE_DESCRIPTION = "Carbon price (currency per tonne CO2e) overriding the profile's for this request."


def _decision_request(profile: Optional[str], carbon_price: Optional[float]) -> DecisionRequest:
    """Validates the per-request ranking parameters (422 on an unknown profile)."""
    if profile:
        try:
            profile = normalize_profile(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DecisionRequest(profile=profile or None, carbon_price=carbon_price)


async def _decide(
    records: List[RecommendationRecord],
    decision: DecisionRequest,
    reco_repo: RecommendationRepository,
    gitops_repo: GitOpsRepository,
    pr_repo: PullRequestRepository,
    service: Optional[AutomationPlanApiService],
    lookup_pool: Sequence[RecommendationRecord] = (),
) -> List[RecommendationRecord]:
    """Attaches the live expected value and eligibility, ordered by expected value.

    ``lookup_pool`` is the unfiltered list ``records`` was cut from (siblings of an action).
    """
    return await decide_records(
        records,
        config=get_config(),
        reco_repo=reco_repo,
        gitops_repo=gitops_repo,
        pr_repo=pr_repo,
        automation_configured=service is not None,
        request=decision,
        lookup_pool=lookup_pool,
    )


@router.get("/recommendations", response_model=List[RecommendationRecord])
async def list_recommendations(
    response: Response,
    namespace: Optional[str] = Depends(validate_namespace),
    profile: Optional[str] = Query(None, description=_PROFILE_DESCRIPTION),
    carbon_price: Optional[float] = Query(None, ge=0, description=_CARBON_PRICE_DESCRIPTION),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
    actor: str = Depends(get_authenticated_actor),
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION, deprecated=True),
):
    """Read active recommendations (and ``pr_open`` ones); never runs analyzers or writes recommendations."""
    decision = _decision_request(profile, carbon_price)
    if refresh:
        await enqueue_refresh(run_repo, response, namespace=namespace, requested_by=actor)
    records = await reco_repo.get_active_recommendations(namespace=namespace, include_pr_open=True)
    return await _decide(records, decision, reco_repo, gitops_repo, pr_repo, service)


@router.get("/recommendations/active", response_model=List[RecommendationRecord])
async def list_active_recommendations(
    response: Response,
    namespace: Optional[str] = Depends(validate_namespace),
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION, deprecated=True),
    source: Optional[str] = Query(None, description="Filter by recommendation source (greenkube, vpa, ...)."),
    risk_level: Optional[str] = Query(None, description="Filter by risk level (low, medium, high)."),
    capability: Optional[str] = Query(None, description="Filter by optimization capability."),
    profile: Optional[str] = Query(None, description=_PROFILE_DESCRIPTION),
    carbon_price: Optional[float] = Query(None, ge=0, description=_CARBON_PRICE_DESCRIPTION),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
    actor: str = Depends(get_authenticated_actor),
):
    """Return active recommendations from the database with their live decision.

    Recommendations whose pull request is open (``pr_open``) are listed too,
    with the ``pull_request_open`` eligibility blocker.

    Ordered by risk-adjusted expected value (``ranking_score``, currency per
    year) under the requested profile and carbon price, then value, projected
    cost and identity. Scores are absolute, so filters never reorder the
    remaining recommendations. Each record carries its ``eligibility``.
    ``refresh=true`` only queues a controller run; it never blocks or writes
    recommendations in the request.
    """
    decision = _decision_request(profile, carbon_price)
    if refresh:
        await enqueue_refresh(run_repo, response, namespace=namespace, requested_by=actor)

    records = await reco_repo.get_active_recommendations(namespace=namespace, include_pr_open=True)
    filtered = _filter_records(records, source, risk_level, capability)
    return await _decide(filtered, decision, reco_repo, gitops_repo, pr_repo, service, lookup_pool=records)


@router.get("/recommendations/top", response_model=List[TopRecommendation])
async def list_top_recommendations(
    response: Response,
    namespace: Optional[str] = Depends(validate_namespace),
    limit: int = Query(5, ge=1, le=50, description="Number of recommendations to return."),
    metric: Literal["co2", "cost", "value"] = Query(
        "co2",
        description="Ranking metric: projected co2 or cost savings, or value (risk-adjusted expected value).",
    ),
    profile: Optional[str] = Query(
        None,
        description=_PROFILE_DESCRIPTION.replace(
            "Defaults to RECOMMENDATION_RANKING_PROFILE.", "When set, ranking uses the expected value."
        ),
    ),
    carbon_price: Optional[float] = Query(None, ge=0, description=_CARBON_PRICE_DESCRIPTION),
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION, deprecated=True),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    run_repo: OptimizationRunRepository = Depends(get_optimization_run_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
    actor: str = Depends(get_authenticated_actor),
):
    """Return the highest-impact active recommendations.

    The default ranking uses projected CO2e savings for the coming year. Pass
    ``metric=cost`` to prioritize direct cloud cost savings, or ``metric=value``
    or a ``profile`` to rank by the risk-adjusted expected value.
    """
    decision = _decision_request(profile, carbon_price)
    if refresh:
        await enqueue_refresh(run_repo, response, namespace=namespace, requested_by=actor)

    if profile or metric == "value" or carbon_price is not None:
        records = await reco_repo.get_active_recommendations(namespace=namespace)
    else:
        records = await reco_repo.get_top_recommendations(limit=limit, savings_metric=metric, namespace=namespace)
    decided = await _decide(records, decision, reco_repo, gitops_repo, pr_repo, service)
    # The records already carry the live value under the requested profile and carbon price:
    # rank them as they are, never re-score them with the defaults.
    ranking_metric = "value" if profile else metric
    return rank_recommendations(decided, limit=limit, savings_metric=ranking_metric)


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
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid ISO 8601 datetime for 'start' or 'end'.",
        ) from exc

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


def _verification(record) -> str:
    status = record.verification_status
    return str(getattr(status, "value", status) or "")


async def get_ledger_repo() -> LedgerRepository:
    """Provide the savings ledger v2 repository (the read model of the savings report, LED-04)."""
    return get_ledger_repository()


@router.get("/recommendations/savings", response_model=SavingsReport)
async def get_savings_summary(
    namespace: Optional[str] = Depends(validate_namespace),
    last: Optional[str] = Query(None, description="Time range (e.g., '24h', '7d', '30d', 'ytd')."),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    savings_repo: SavingsLedgerRepository = Depends(get_savings_ledger_repository),
    ledger_repo: LedgerRepository = Depends(get_ledger_repo),
):
    """Return projected, measured and realized savings as separate blocks.

    ``projected_annual`` and ``run_rate_annual`` are current yearly rates;
    ``realized_to_date`` sums measured ledger accruals over the ``last`` window
    (all time when omitted). Projected and user-declared accruals are reported
    in their own blocks and never added to realized figures. The endpoint
    answers 503 instead of falling back when the ledger cannot be read.
    """
    start, end = _get_optional_time_range(last)
    try:
        applied = await reco_repo.get_applied_recommendations(namespace=namespace)
    except Exception as exc:
        logger.error("Could not load applied recommendations for the savings report: %s", exc)
        raise HTTPException(status_code=503, detail="Recommendation store unavailable.") from exc
    try:
        # IMP-07: the opportunity of the active recommendations, one figure for every page.
        active = list(await reco_repo.get_active_recommendations(namespace=namespace, include_pr_open=True))
    except Exception as exc:
        logger.warning("Could not load active recommendations for the savings opportunity: %s", exc)
        active = None
    accrued_only = None
    unverified = None
    fixes = None
    interim = None
    interim_accrued = None
    source = "ledger_v2"
    try:
        cluster_name = _get_savings_cluster_name()
        identity = await ledger_repo.get_identity(cluster_name)
        if identity is not None:
            # LED-04: the hash-chained ledger v2 is the single read model (restated totals).
            breakdown = await ledger_repo.accrual_breakdown(identity[0], start, end, namespace=namespace)
            accrued_only = await ledger_repo.accrual_breakdown(
                identity[0], start, end, namespace=namespace, books=("accrued",)
            )
            inconclusive = [
                int(record.id)
                for record in applied
                if record.id is not None and _verification(record) == "inconclusive"
            ]
            unverified = await ledger_repo.accrual_breakdown(
                identity[0], start, end, namespace=namespace, recommendation_ids=inconclusive
            )
            fixes = await ledger_repo.accrual_breakdown(
                identity[0], start, end, namespace=namespace, reasons=("cost_of_fix",)
            )
            # MEA-16: interim-method verdicts are reported apart from the measured totals.
            interim_ids = interim_recommendation_ids(applied)
            interim = (
                await ledger_repo.accrual_breakdown(
                    identity[0], start, end, namespace=namespace, recommendation_ids=interim_ids
                )
                if interim_ids
                else []
            )
            interim_accrued = (
                await ledger_repo.accrual_breakdown(
                    identity[0], start, end, namespace=namespace, recommendation_ids=interim_ids, books=("accrued",)
                )
                if interim_ids
                else []
            )
        else:
            source = "ledger_v1"
            breakdown = await savings_repo.get_accrual_breakdown(cluster_name, start, end, namespace=namespace)
            # MEA-16: interim verdicts are reported apart in the v1 fallback too.
            interim_ids = interim_recommendation_ids(applied)
            interim = (
                await savings_repo.get_accrual_breakdown(
                    cluster_name, start, end, namespace=namespace, recommendation_ids=interim_ids
                )
                if interim_ids
                else []
            )
        legacy = await savings_repo.has_legacy_compressed_rows(cluster_name, start, end, namespace=namespace)
    except Exception as exc:
        logger.error("Could not read the savings ledger: %s", exc)
        raise HTTPException(status_code=503, detail="Savings ledger unavailable.") from exc
    return assemble_savings_report(
        applied,
        breakdown,
        namespace=namespace,
        start=start,
        end=end,
        legacy_compressed=legacy,
        accrued_only=accrued_only,
        source=source,
        projected_unverified=unverified,
        cost_of_fix=fixes,
        interim=interim,
        active=active,
        interim_accrued_only=interim_accrued,
    )


#: CSV columns of the recommendation export: projected cost (billable and allocated) and projected
#: released capacity, each in its own unit (WP-08.1). Released capacity is never money.
EXPORT_COLUMNS = (
    "id",
    "type",
    "namespace",
    "owner_kind",
    "owner_name",
    "container_name",
    "status",
    "description",
    "currency",
    "projected_billable_cost_annual",
    "projected_allocated_cost_annual",
    "projected_allocated_co2e_annual_g",
    "realization_factor",
    "projection_note",
    "capacity_method",
    "capacity_direction",
    "released_cpu_cores",
    "released_mem_gib",
    "released_cpu_cores_per_replica",
    "released_mem_gib_per_replica",
    "replicas",
    "released_cpu_core_hours_per_year",
    "released_mem_gib_hours_per_year",
    "affected_replica_hours_per_year",
    "released_node_hours_per_year",
    "released_storage_gib",
    "released_load_balancers",
)


def _export_row(record: RecommendationRecord) -> dict:
    impact = record.impact if isinstance(record.impact, dict) else {}
    capacity = record.released_capacity or {}
    annual = capacity.get("annual") or {}
    now = capacity.get("instantaneous") or {}
    return {
        "id": record.id,
        "type": _enum_value(record.type),
        "namespace": record.namespace,
        "owner_kind": record.owner_kind,
        "owner_name": record.owner_name,
        "container_name": record.container_name,
        "status": _enum_value(record.status),
        "description": record.description,
        "currency": impact.get("currency"),
        "projected_billable_cost_annual": record.projected_billable_cost_annual,
        "projected_allocated_cost_annual": record.projected_allocated_cost_annual,
        "projected_allocated_co2e_annual_g": record.projected_allocated_co2e_annual_g,
        "realization_factor": impact.get("realization_factor"),
        "projection_note": record.projection_note,
        "capacity_method": capacity.get("method"),
        "capacity_direction": capacity.get("direction"),
        "released_cpu_cores": now.get("cpu_cores"),
        "released_mem_gib": now.get("mem_gib"),
        "released_cpu_cores_per_replica": now.get("cpu_cores_per_replica"),
        "released_mem_gib_per_replica": now.get("mem_gib_per_replica"),
        "replicas": now.get("replicas"),
        "released_cpu_core_hours_per_year": annual.get("cpu_core_hours"),
        "released_mem_gib_hours_per_year": annual.get("mem_gib_hours"),
        "affected_replica_hours_per_year": annual.get("replica_hours"),
        "released_node_hours_per_year": annual.get("node_hours"),
        "released_storage_gib": now.get("storage_gib"),
        "released_load_balancers": now.get("load_balancers"),
    }


def _csv_cell(value) -> str:
    """Prevent CSV formula injection, including leading-whitespace bypasses."""
    text = str(value) if value is not None else ""
    return f"'{text}" if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) and not _is_number(text) else text


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


@router.get("/recommendations/export")
async def export_recommendations(
    namespace: Optional[str] = Depends(validate_namespace),
    fmt: Literal["csv", "json"] = Query("csv", alias="format", description="Export format: 'csv' or 'json'."),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Active recommendations as CSV or JSON, with their projected cost and released capacity (WP-08.1).

    Every figure is projected. Cost columns are per year in ``currency``;
    released capacity columns carry their unit in their name (cores, GiB,
    core-hours, GiB-hours and node-hours per year, storage GiB,
    LoadBalancer count). Released capacity is never converted to money and
    never added to a cost; negative values are increases. The JSON form returns
    the full records, including the ``released_capacity`` object with its units.
    """
    try:
        records = await reco_repo.get_active_recommendations(namespace=namespace)
    except Exception as exc:
        logger.error("Could not load active recommendations for the export: %s", exc)
        raise HTTPException(status_code=503, detail="Recommendation store unavailable.") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if fmt == "json":
        payload = json.dumps([record.model_dump(mode="json") for record in records], default=str)
        return Response(
            content=payload,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="greenkube-recommendations-{stamp}.json"'},
        )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(EXPORT_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({key: _csv_cell(value) for key, value in _export_row(record).items()})
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="greenkube-recommendations-{stamp}.csv"'},
    )


@router.get("/recommendations/{rec_id}", response_model=RecommendationRecord)
async def get_recommendation_detail(
    rec_id: int,
    profile: Optional[str] = Query(None, description=_PROFILE_DESCRIPTION),
    carbon_price: Optional[float] = Query(None, ge=0, description=_CARBON_PRICE_DESCRIPTION),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
):
    """Return a single recommendation with its full evidence block and assessment.

    This is the review endpoint: it returns the observation window, utilization
    distribution, proposed change, confidence, risk factors, rollback conditions
    and expiry needed to decide without re-running the analysis, plus the live
    expected value and eligibility.
    """
    decision = _decision_request(profile, carbon_price)
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    (decided,) = await _decide([record], decision, reco_repo, gitops_repo, pr_repo, service)
    return decided


def get_change_recorder_factory():
    """Builder of the change-measurement service that records a manual apply as a change (MEA-16).

    ``None`` when ``CHANGE_MEASUREMENT_ENABLED`` is off: the interim verifier
    then judges manual applies.
    """
    config = get_config()
    if not getattr(config, "CHANGE_MEASUREMENT_ENABLED", True):
        return None

    def build(lifecycle):
        from greenkube.core.factory import get_combined_metrics_repository
        from greenkube.storage.change_repository import get_change_repository
        from greenkube.verify.drift import kubernetes_workload_snapshot
        from greenkube.verify.service import ChangeMeasurementService

        return ChangeMeasurementService(
            lifecycle,
            get_combined_metrics_repository(),
            get_change_repository(),
            config=config,
            workload_reader=kubernetes_workload_snapshot,
        )

    return build


@router.patch(
    "/recommendations/{rec_id}/apply",
    dependencies=[Depends(require_scope(Scope.WRITE))],
    response_model=RecommendationRecord,
)
async def apply_recommendation(
    rec_id: int,
    request: ApplyRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
    change_recorder=Depends(get_change_recorder_factory),
):
    """Mark a recommendation as applied.

    Optionally provide the actual values applied (CPU/memory). The projected
    savings are always estimated server-side; savings provided in the request
    are validated and recorded separately as user-declared savings.

    A recommendation is considered applied even when the actual value deviates from
    the recommendation (e.g., reducing CPU to 50m instead of the suggested 40m).
    Applying freezes the verification baseline and starts the observation window.
    Only ``active`` and ``pr_open`` recommendations can be applied (409 otherwise).

    With change measurement on, the apply is recorded as a change (application
    method ``manual``, MEA-16) with its baseline snapshot and measured by the
    change pipeline like a detected change; if recording fails, the lifecycle
    job records it from the change id stored on the recommendation.
    """
    import uuid

    from greenkube.core.optimization.lifecycle import RecommendationLifecycle, RecommendationTransitionError

    lifecycle = RecommendationLifecycle(reco_repo)
    change_id = uuid.uuid4().hex if change_recorder is not None else None
    declared = await _validate_declared_savings(rec_id, request, reco_repo)
    try:
        # Declared savings never reach the projection columns: those keep the
        # server-side estimate, and declarations live in user_declared_savings.
        applied = await lifecycle.apply(
            rec_id,
            actual_cpu=request.actual_cpu_request_millicores,
            actual_memory=request.actual_memory_request_bytes,
            carbon_saved_co2e_grams=None,
            cost_saved=None,
            application_method=request.application_method or "manual",
            actor=actor,
            change_id=change_id,
        )
    except RecommendationTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if change_recorder is not None and change_id is not None:
        try:
            await change_recorder(lifecycle).record_change(change_id, [applied])
        except Exception as exc:
            # The record carries the change id: the lifecycle job's repair records the change.
            logger.warning("Change %s of recommendation %s is recorded later: %s", change_id, rec_id, exc)
    if declared is not None:
        try:
            applied = await record_user_declared_savings(reco_repo, applied, declared)
        except Exception as exc:
            logger.error("Recommendation %s applied, but its declared savings were not stored: %s", rec_id, exc)
            raise HTTPException(
                status_code=503,
                detail=(
                    "The recommendation was applied with the projected savings, but the declared savings "
                    "could not be stored. Retry the request to record them."
                ),
            ) from exc
    return applied


async def _validate_declared_savings(rec_id: int, request: ApplyRecommendationRequest, reco_repo):
    """Validate user-declared savings (C0.8): finite, within 10x the projection, signed but flagged."""
    if request.carbon_saved_co2e_grams is None and request.cost_saved is None:
        return None
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    try:
        return validate_user_declared_savings(
            record,
            co2e_grams_per_year=request.carbon_saved_co2e_grams,
            cost_per_year=request.cost_saved,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/recommendations/{rec_id}/remeasure",
    dependencies=[Depends(require_scope(Scope.WRITE))],
    response_model=RecommendationRecord,
    status_code=202,
)
async def remeasure_recommendation(
    rec_id: int,
    request: Optional[RemeasureRequest] = None,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
):
    """Start a new fixed-horizon measurement of an applied recommendation.

    Allowed when the latest measurement was ``not_realized`` (``verification_status=failed``)
    or ``inconclusive``. The previous measurement stays immutable; the new one starts now,
    lasts ``VERIFICATION_WINDOW_HOURS`` and supersedes it. Returns 409 otherwise.
    """
    from greenkube.core.optimization.lifecycle import (
        RecommendationLifecycle,
        RecommendationNotFoundError,
        RecommendationTransitionError,
    )

    try:
        return await RecommendationLifecycle(reco_repo).request_remeasure(
            rec_id,
            reason=request.reason if request is not None else None,
            actor=actor,
        )
    except RecommendationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RecommendationTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post(
    "/recommendations/{rec_id}/rollback-review/dismiss",
    dependencies=[Depends(require_scope(Scope.WRITE))],
    response_model=RecommendationRecord,
)
async def dismiss_rollback_review(
    rec_id: int,
    request: DismissRollbackReviewRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
):
    """Dismiss a false-positive rollback review; the change is measured again from now.

    Requires a reason, recorded in the audit trail. Returns 409 when the recommendation
    is not in ``rollback_review``.
    """
    from greenkube.core.optimization.lifecycle import (
        RecommendationLifecycle,
        RecommendationNotFoundError,
        RecommendationTransitionError,
    )

    try:
        return await RecommendationLifecycle(reco_repo).dismiss_rollback_review(
            rec_id, reason=request.reason, actor=actor
        )
    except RecommendationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RecommendationTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # Invalid input the body validation let through (e.g. a blank reason): never a 500.
        raise HTTPException(status_code=422, detail=str(e))


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


@router.patch(
    "/recommendations/{rec_id}/ignore",
    dependencies=[Depends(require_scope(Scope.WRITE))],
    response_model=RecommendationRecord,
)
async def ignore_recommendation(
    rec_id: int,
    request: IgnoreRecommendationRequest,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
):
    """Permanently ignore a recommendation.

    The recommendation will no longer appear in the active list. It remains visible
    under GET /recommendations/ignored so it can be reviewed or un-ignored later.

    Typical use case: a pod cannot support HPA due to a RWO PVC, or a namespace
    intentionally runs at low utilization (e.g., a staging environment).
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle, RecommendationTransitionError

    try:
        # Compare-and-swap with its event; an open PR or an applied change is refused (409).
        return await RecommendationLifecycle(reco_repo).ignore(rec_id, reason=request.reason, actor=actor)
    except RecommendationTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete(
    "/recommendations/{rec_id}/ignore",
    dependencies=[Depends(require_scope(Scope.WRITE))],
    response_model=RecommendationRecord,
)
async def unignore_recommendation(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    actor: str = Depends(get_authenticated_actor),
):
    """Restore an ignored recommendation back to active status.

    Useful when circumstances change (e.g., the PVC is migrated to RWX) or when a
    recommendation was accidentally ignored.
    """
    from greenkube.core.optimization.lifecycle import RecommendationLifecycle, RecommendationTransitionError

    try:
        # Only an ignored recommendation is restored; any other status is a 409.
        return await RecommendationLifecycle(reco_repo).unignore(rec_id, actor=actor)
    except RecommendationTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/sources/track-record")
async def get_source_track_record(
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
):
    """Measured realization rate per source family and capability (roadmap I3).

    Each entry is a Beta(alpha, beta) distribution with a uniform prior:
    ``realized`` outcomes add to alpha, ``not_realized`` and ``rollback_review``
    to beta; ``inconclusive`` outcomes are only counted. ``p_success`` of a
    recommendation combines its confidence with the entry of its source and
    capability.
    """
    from greenkube.core.optimization.track_record import PRIOR_ALPHA, PRIOR_BETA

    book = await load_track_record(reco_repo)
    return {
        "generated_at": datetime.now(timezone.utc),
        "method": book.method,
        "prior": {"alpha": PRIOR_ALPHA, "beta": PRIOR_BETA},
        "entries": [entry.to_dict() for entry in book.entries()],
    }
