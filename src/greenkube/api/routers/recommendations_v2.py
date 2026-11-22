# src/greenkube/api/routers/recommendations_v2.py
"""Recommendation API v2 (roadmap E4, E6, E7).

Endpoints (prefix ``/api/v2``):
  GET /recommendations        - Active recommendations in the v2 shape
                                (?namespace=&status=&capability=&kind=&source=&limit=)
  GET /recommendations/{id}   - One recommendation with its revision history

The v2 shape is built around the canonical target, a typed ``proposal``, the
``consensus`` across sources (policy, agreement, contested flag, per-source
spread), the supporting ``signals``, the projected ``impact``, ``risk``,
``value`` and the delivery ``eligibility``. ``fingerprint`` is the
cluster-qualified v4 identity and ``revision`` increases each time the
consolidated proposal changes; ``queued_revision`` is newer advice waiting
behind an open plan ("newer advice available").

The v1 API (``/api/v1/recommendations``) keeps its flat fields: they are the
same proposal in flat form. Reads only: nothing here runs analyzers or writes.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_automation_plan_service,
    get_gitops_binding_repository,
    get_pull_request_repository,
    get_recommendation_repository,
    validate_namespace,
)
from greenkube.api.routers.recommendations import _decide, _decision_request
from greenkube.core.config import get_config
from greenkube.core.optimization.registry import cluster_uid
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.recommendation_v2 import RecommendationV2, to_v2
from greenkube.signals.mapping import capability_for
from greenkube.storage.base_gitops_repository import GitOpsRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_LIMIT = 500


class RecommendationV2List(BaseModel):
    """A page of v2 recommendations, ordered by expected value."""

    items: List[RecommendationV2] = Field(default_factory=list)
    #: Matching recommendations before ``limit``.
    total: int = 0


def _status_values(status: Optional[List[str]]) -> Optional[List[str]]:
    if not status:
        return None
    values = []
    for item in status:
        for value in str(item).split(","):
            value = value.strip()
            if not value:
                continue
            try:
                values.append(RecommendationStatus(value).value)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Unknown recommendation status '{value}'.") from exc
    return values or None


def _kind_values(kind: Optional[List[str]]) -> Optional[frozenset]:
    """Finding kinds (the v1 types, case-insensitive) of the ``kind`` filter; ``422`` when unknown."""
    if not kind:
        return None
    known = {item.value.upper(): item.value for item in RecommendationType}
    values = set()
    for item in kind:
        for value in str(item).split(","):
            value = value.strip()
            if not value:
                continue
            if value.upper() not in known:
                raise HTTPException(status_code=422, detail=f"Unknown recommendation kind '{value}'.")
            values.add(known[value.upper()])
    return frozenset(values) or None


def _matches(
    record: RecommendationRecord,
    capability: Optional[str],
    source: Optional[str],
    kinds: Optional[frozenset] = None,
) -> bool:
    if kinds is not None and str(getattr(record.type, "value", record.type) or "") not in kinds:
        return False
    if capability and capability not in (
        capability_for(record.type).value,
        str(getattr(record.capability, "value", record.capability) or ""),
    ):
        return False
    if source:
        own = str(getattr(record.source, "value", record.source) or "")
        provenance = record.consensus.get("source_ids") if isinstance(record.consensus, dict) else None
        if source != own and source not in record.sources and source not in (provenance or []):
            return False
    return True


@router.get("/recommendations", response_model=RecommendationV2List)
async def list_recommendations_v2(
    namespace: Optional[str] = Depends(validate_namespace),
    status: Optional[List[str]] = Query(
        None, description="Lifecycle statuses (repeat or comma-separate). Default: active and pr_open."
    ),
    capability: Optional[str] = Query(
        None, description="v2 capability (container_requests, schedule, cleanup...) or v1 capability."
    ),
    kind: Optional[List[str]] = Query(
        None, description="Finding kinds, the v1 types (RIGHTSIZING_CPU...); repeat or comma-separate."
    ),
    source: Optional[str] = Query(None, description="Only recommendations this source supports."),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    profile: Optional[str] = Query(None, description="Ranking profile of the expected value."),
    carbon_price: Optional[float] = Query(None, ge=0, description="Carbon price overriding the profile's."),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
) -> RecommendationV2List:
    """Recommendations in the v2 shape with their live expected value and eligibility."""
    decision = _decision_request(profile, carbon_price)
    statuses = _status_values(status)
    kinds = _kind_values(kind)
    if statuses is None:
        records = await reco_repo.get_active_recommendations(namespace=namespace, include_pr_open=True)
    else:
        records = await reco_repo.get_recommendations_by_statuses(statuses, namespace=namespace)
    records = [record for record in records if _matches(record, capability, source, kinds)]
    decided = await _decide(records, decision, reco_repo, gitops_repo, pr_repo, service)
    page = decided[:limit]
    queued = await reco_repo.get_queued_revisions([record.id for record in page if record.id is not None])
    uid = cluster_uid(get_config())
    items = [
        to_v2(record, uid, queued_revision=queued[record.id].revision if record.id in queued else None)
        for record in page
    ]
    return RecommendationV2List(items=items, total=len(decided))


@router.get("/recommendations/{rec_id}", response_model=RecommendationV2)
async def get_recommendation_v2(
    rec_id: int = Path(..., ge=1),
    profile: Optional[str] = Query(None, description="Ranking profile of the expected value."),
    carbon_price: Optional[float] = Query(None, ge=0, description="Carbon price overriding the profile's."),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    gitops_repo: GitOpsRepository = Depends(get_gitops_binding_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: Optional[AutomationPlanApiService] = Depends(get_automation_plan_service),
) -> RecommendationV2:
    """One recommendation in the v2 shape, with its revision history (newest first)."""
    decision = _decision_request(profile, carbon_price)
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    (decided,) = await _decide([record], decision, reco_repo, gitops_repo, pr_repo, service)
    revisions = await reco_repo.get_revisions(rec_id)
    queued = next((revision.revision for revision in revisions if revision.state == "queued"), None)
    return to_v2(decided, cluster_uid(get_config()), queued_revision=queued, revisions=revisions)
