# src/greenkube/api/routers/automation.py
"""API routes for the recommendation pull-request bot (Phase 4).

Endpoints:
  POST /recommendations/{id}/apply-pr        - Preview or open a pull request
  GET  /recommendations/{id}/pull-requests   - List PR attempts for a recommendation
  GET  /automation/status                    - Feature readiness for the UI
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from greenkube.api.dependencies import get_pull_request_repository, get_recommendation_repository
from greenkube.automation.service import AutomationService
from greenkube.models.metrics import ApplyPrRequest, ApplyPrResponse, PullRequestRecord
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_service(
    reco_repo: RecommendationRepository,
    pr_repo: PullRequestRepository,
) -> AutomationService:
    return AutomationService(reco_repo=reco_repo, pr_repo=pr_repo)


@router.post("/recommendations/{rec_id}/apply-pr", response_model=ApplyPrResponse)
async def apply_recommendation_pr(
    rec_id: int,
    request: ApplyPrRequest | None = None,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Open a pull request that applies a rightsizing recommendation.

    With ``dry_run=true`` the endpoint resolves the source, patches the manifest
    in memory and returns the diff without touching Git. Otherwise it creates a
    branch, commits the patch and opens the pull request, then tracks it in
    ``recommendation_pull_requests`` and moves the recommendation to ``pr_open``.
    """
    service = _build_service(reco_repo, pr_repo)
    try:
        response = await service.apply_recommendation_pr(rec_id, request or ApplyPrRequest())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if response.status == "error":
        raise HTTPException(status_code=422, detail=response.message or "Pull request could not be opened.")
    return response


@router.get("/recommendations/{rec_id}/pull-requests", response_model=List[PullRequestRecord])
async def list_recommendation_pull_requests(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return every pull-request attempt recorded for a recommendation."""
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    return await pr_repo.get_pull_requests_for_recommendation(rec_id)


@router.get("/automation/status")
async def automation_status(
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return whether the PR bot is configured and which provider it targets."""
    return await _build_service(reco_repo, pr_repo).automation_status()
