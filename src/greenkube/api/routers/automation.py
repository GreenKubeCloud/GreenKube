# src/greenkube/api/routers/automation.py
"""API routes for the recommendation pull-request bot (Phase 4).

Endpoints:
  POST /recommendations/{id}/apply-pr        - Preview or open a pull request
  GET  /recommendations/{id}/pull-requests   - List PR attempts for a recommendation
  GET  /automation/status                    - Feature readiness for the UI
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from greenkube.api.dependencies import get_pull_request_repository, get_recommendation_repository
from greenkube.automation.service import AutomationService
from greenkube.automation.source_resolver import SourceResolutionError
from greenkube.models.metrics import ApplyPrRequest, PullRequestRecord
from greenkube.storage.base_automation_operation_repository import AutomationOperationRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_service(
    reco_repo: RecommendationRepository,
    pr_repo: PullRequestRepository,
) -> AutomationService:
    return AutomationService(reco_repo=reco_repo, pr_repo=pr_repo)


async def _get_operation_repository() -> AutomationOperationRepository:
    from greenkube.core.config import get_config
    from greenkube.core.db import get_db_manager

    if get_config().DB_TYPE == "postgres":
        from greenkube.storage.postgres.automation_operation_repository import PostgresAutomationOperationRepository

        return PostgresAutomationOperationRepository(get_db_manager())
    from greenkube.storage.sqlite.automation_operation_repository import SQLiteAutomationOperationRepository

    return SQLiteAutomationOperationRepository(get_db_manager())


@router.post("/recommendations/{rec_id}/apply-pr", response_model=None)
async def apply_recommendation_pr(
    rec_id: int,
    response: Response,
    request: ApplyPrRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    audit_actor: str = Header(default="user", alias="X-Audit-Actor"),
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    operation_repo: AutomationOperationRepository = Depends(_get_operation_repository),
):
    """Open a pull request that applies a rightsizing recommendation.

    With ``dry_run=true`` the endpoint resolves the source, patches the manifest
    in memory and returns the diff without touching Git. Otherwise it creates a
    branch, commits the patch and opens the pull request, then tracks it in
    ``recommendation_pull_requests`` and moves the recommendation to ``pr_open``.
    """
    service = _build_service(reco_repo, pr_repo)
    try:
        request = request or ApplyPrRequest()
        if request.dry_run:
            result = await service.apply_recommendation_pr(rec_id, request, actor=audit_actor)
        else:
            operation = await service.enqueue_recommendation_pr(
                rec_id, request, operation_repo, actor=audit_actor, idempotency_key=idempotency_key
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return {
                "status": operation.status.value,
                "operation_id": operation.id,
                "recommendation_id": operation.recommendation_id,
                "idempotency_key": operation.idempotency_key,
                "fingerprint": operation.fingerprint,
                "actor": operation.actor,
            }
    except ValueError as e:
        message = str(e)
        raise HTTPException(status_code=404 if "not found" in message.lower() else 422, detail=message)
    if result.status == "error":
        raise HTTPException(status_code=422, detail=result.message or "Pull request could not be opened.")
    return result


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


@router.get("/recommendations/{rec_id}/apply-pr/eligibility")
async def recommendation_pr_eligibility(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Check whether a recommendation can be applied through GitOps PR automation."""
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    if record.type.value not in {"RIGHTSIZING_CPU", "RIGHTSIZING_MEMORY"}:
        return {"eligible": False, "reason": "Only CPU and memory rightsizing supports pull requests."}
    if record.scope != "workload" or not record.owner_kind or not record.owner_name:
        return {
            "eligible": False,
            "reason": "Only workload-scoped recommendations can be applied through a pull request.",
        }
    if await pr_repo.get_open_pull_requests(record.id):
        return {"eligible": False, "reason": "A pull request is already open for this recommendation."}
    try:
        source = await _build_service(reco_repo, pr_repo).resolver.resolve(record)
    except SourceResolutionError as exc:
        return {"eligible": False, "reason": str(exc)}
    return {"eligible": True, "repo": source.repo_url, "path": source.path, "branch": source.branch}


@router.get("/automation/pull-requests", response_model=List[PullRequestRecord])
async def list_open_pull_requests(
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return pull requests that are still pending or open."""
    return await pr_repo.get_open_pull_requests()


@router.get("/automation/status")
async def automation_status(
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return whether the PR bot is configured and which provider it targets."""
    return await _build_service(reco_repo, pr_repo).automation_status()


@router.get("/automation/operations/{operation_id}")
async def get_automation_operation(
    operation_id: int,
    operation_repo: AutomationOperationRepository = Depends(_get_operation_repository),
):
    """Return queue state and immutable integrity metadata for an operation."""
    operation = await operation_repo.get_by_id(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail=f"Automation operation {operation_id} not found.")
    return {
        "id": operation.id,
        "recommendation_id": operation.recommendation_id,
        "status": operation.status.value,
        "idempotency_key": operation.idempotency_key,
        "fingerprint": operation.fingerprint,
        "preview_digest": operation.preview_digest,
        "commit_digest": operation.commit_digest,
        "attempts": operation.attempts,
        "error": operation.error,
        "result": operation.result,
        "actor": operation.actor,
    }
