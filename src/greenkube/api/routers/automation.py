"""Compatibility routes backed by the server-resolved GitOps plan workflow."""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, List
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_authenticated_actor,
    get_automation_plan_service,
    get_gitops_binding_repository,
    get_pull_request_repository,
    get_recommendation_repository,
)
from greenkube.api.routers.plans import _domain_error_response, _safe_repository_url
from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import AutomationErrorCode, Operation, Plan, PlanStatus
from greenkube.models.metrics import ApplyPrRequest, ApplyPrResponse, PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import GitOpsRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository

router = APIRouter()
WORKER_STALE_AFTER = timedelta(seconds=120)


@router.post("/recommendations/{rec_id}/apply-pr", response_model=None)
async def apply_recommendation_pr(
    rec_id: int,
    response: Response,
    http_request: Request,
    request: ApplyPrRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> ApplyPrResponse | dict[str, object] | JSONResponse:
    """Keep the legacy request shape without requiring source annotations."""
    if service is None:
        return _service_unavailable()
    audit_actor = get_authenticated_actor(http_request)
    request = request or ApplyPrRequest()
    try:
        existing_pr: PullRequestRecord | None = None
        if request.dry_run:
            plan = await service.preview_plan([rec_id])
        else:
            open_prs = [
                item for item in await pr_repo.get_open_pull_requests(rec_id) if item.status is PullRequestStatus.OPEN
            ]
            if len(open_prs) > 1:
                raise AutomationPlanError(
                    AutomationErrorCode.PLAN_STALE,
                    "multiple open pull requests already exist for this recommendation",
                )
            if open_prs:
                existing_pr = open_prs[0]
                if not existing_pr.plan_id:
                    raise AutomationPlanError(
                        AutomationErrorCode.PLAN_STALE,
                        "an open pull request exists without a linked automation plan",
                    )
                plan = await service.read_plan(existing_pr.plan_id)
                if plan is None:
                    raise AutomationPlanError(
                        AutomationErrorCode.PLAN_STALE,
                        "an open pull request exists but its automation plan is unavailable",
                    )
            else:
                plan = await service.prepare_plan([rec_id], actor=audit_actor)
        if request.base_branch and request.base_branch != plan.source.ref:
            raise AutomationPlanError(
                AutomationErrorCode.UNSUPPORTED_SOURCE,
                "base_branch overrides are not accepted; use the GitOps-resolved source ref.",
            )
        if request.dry_run:
            return _compatibility_preview(plan)
        if existing_pr is not None:
            return _compatibility_pull_request(plan, existing_pr)

        existing_pr = await pr_repo.get_pull_request_by_plan_id(plan.plan_id)
        if existing_pr is not None and existing_pr.status is PullRequestStatus.OPEN:
            return _compatibility_pull_request(plan, existing_pr)

        operation = await service.read_operation_for_plan(plan.plan_id)
        if operation is None:
            if plan.status not in {PlanStatus.VALIDATED, PlanStatus.APPROVED} or plan.digest is None:
                raise AutomationPlanError(
                    AutomationErrorCode.PLAN_STALE,
                    f"plan cannot be queued from status {plan.status.value}",
                )
            plan = await service.approve_plan(plan.plan_id, plan.digest, actor=audit_actor)
            operation = await service.read_operation_for_plan(plan.plan_id)
        if operation is None:
            return _service_unavailable(
                "The plan was approved but no durable operation was created; inspect the plan worker configuration."
            )
        response.status_code = status.HTTP_202_ACCEPTED
        return _compatibility_operation_response(plan, operation, rec_id, audit_actor)
    except AutomationPlanError as error:
        return _domain_error_response(error)


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
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Check whether the GitOps plan service can prepare this recommendation."""
    if service is None:
        return {"eligible": False, "reason": "The GitOps plan service is not configured."}
    try:
        plan = await service.preview_plan([rec_id])
    except AutomationPlanError as error:
        result: dict[str, object] = {"eligible": False, "reason": error.detail, "code": error.code.value}
        if error.policy_result is not None:
            result["policy_result"] = error.policy_result
        return result
    if plan.status not in {PlanStatus.DRAFT, PlanStatus.VALIDATED}:
        return {"eligible": False, "reason": f"An automation plan is already {plan.status.value}."}
    if await pr_repo.get_open_pull_requests(rec_id):
        return {"eligible": False, "reason": "A pull request is already open for this recommendation."}
    return {
        "eligible": True,
        "plan_id": plan.plan_id,
        "repo": _safe_repository_url(plan.source.repo_url),
        "path": plan.source.path,
        "branch": plan.source.ref,
        "policy_result": plan.policy_result,
    }


@router.get("/automation/pull-requests", response_model=List[PullRequestRecord])
async def list_open_pull_requests(
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return pull requests that are still pending or open."""
    return await pr_repo.get_open_pull_requests()


@router.get("/automation/status")
async def automation_status(
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
    repository: GitOpsRepository = Depends(get_gitops_binding_repository),
):
    """Return secret-free automation readiness, worker health and queue depth."""
    from greenkube.core.config import get_config

    config = get_config()
    configured = service is not None
    enabled = configured and bool(config.AUTOMATION_ENABLED) and config.AUTOMATION_MODE != "disabled"
    now = datetime.now(timezone.utc)

    backlog = None
    coverage = None
    providers: list[dict[str, Any]] = []
    try:
        backlog = await repository.get_automation_backlog_summary(stale_before=now - WORKER_STALE_AFTER)
    except Exception:
        backlog = None
    try:
        coverage = await repository.get_workload_binding_coverage(
            cluster=config.CLUSTER_NAME or "default",
            now=now,
        )
    except Exception:
        coverage = None
    try:
        configured_providers = await repository.list_provider_configs()
    except Exception:
        configured_providers = []
    for provider in configured_providers:
        if provider.id == "unresolved":
            continue
        providers.append(
            {
                "id": provider.id,
                "type": provider.provider_type,
                "enabled": provider.enabled,
                "status": provider.status,
                "capabilities": dict(provider.capabilities),
                "last_checked_at": _iso_utc(provider.last_checked_at),
                "last_error_code": provider.last_error_code,
                "api_host": _safe_host(provider.api_base_url),
            }
        )

    latest_heartbeat = None
    worker_healthy = False
    if backlog is not None and backlog.latest_worker_heartbeat_timestamp > 0:
        latest_heartbeat = datetime.fromtimestamp(backlog.latest_worker_heartbeat_timestamp, tz=timezone.utc)
        worker_healthy = now - latest_heartbeat <= WORKER_STALE_AFTER
    discovery_age = None
    if coverage is not None and coverage.last_refresh is not None:
        discovery_age = max(0.0, (now - _aware_utc(coverage.last_refresh)).total_seconds())

    renderer_url = os.getenv("GITOPS_RENDERER_URL", "").strip()
    queue = (
        {
            "queued": backlog.queued_operations,
            "running": backlog.running_operations,
            "retrying": backlog.retrying_operations,
            "failed": backlog.failed_operations,
            "stuck": backlog.stuck_operations,
            "pending_webhooks": backlog.pending_webhooks,
            "stuck_webhooks": backlog.stuck_webhooks,
        }
        if backlog is not None
        else None
    )
    return {
        "enabled": enabled,
        "ready": enabled and (worker_healthy if config.AUTOMATION_MODE != "disabled" else False),
        "token_configured": configured,
        "provider": "gitops" if configured else None,
        "mode": config.AUTOMATION_MODE,
        "worker": {
            "healthy": worker_healthy,
            "heartbeat_at": latest_heartbeat,
            "count": backlog.worker_count if backlog is not None else None,
            "stale": backlog.stale_workers if backlog is not None else None,
        },
        "queue": queue,
        "plans": (
            {
                "applying": backlog.applying_plans,
                "merged": backlog.merged_plans,
                "stuck": backlog.stuck_plans,
                "sync_failed": backlog.sync_failed_plans,
            }
            if backlog is not None
            else None
        ),
        "discovery": (
            {
                "last_refresh": _iso_utc(coverage.last_refresh),
                "age_seconds": discovery_age,
                "resolved": coverage.resolved,
                "total": coverage.total,
                "coverage_ratio": coverage.ratio,
            }
            if coverage is not None
            else None
        ),
        "renderer": {
            "configured": bool(renderer_url),
            "isolated": bool(renderer_url),
            "egress_firewall": _env_truthy(os.getenv("GITOPS_RENDERER_EGRESS_FIREWALL")),
        },
        "providers": providers,
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _safe_host(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return urlsplit(value.strip()).hostname
    except ValueError:
        return None


def _env_truthy(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


@router.get("/automation/operations/{operation_id}")
async def get_automation_operation(
    operation_id: str,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Expose plan-operation state through the legacy operation endpoint."""
    if service is None:
        return _service_unavailable()
    operation = await service.read_operation(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail=f"Automation operation {operation_id} not found.")
    plan = await service.read_plan(operation.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Automation plan {operation.plan_id} not found.")
    pull_request = await pr_repo.get_pull_request_by_plan_id(operation.plan_id)
    return _operation_response(operation, plan, pull_request)


def _compatibility_preview(plan: Plan) -> ApplyPrResponse:
    if plan.file_plan is None:
        raise AutomationPlanError(
            AutomationErrorCode.RENDER_VALIDATION_FAILED,
            "the GitOps plan has no validated file diff",
        )
    return ApplyPrResponse(
        status="dry_run",
        policy_result=plan.policy_result,
        provider=plan.source.provider_id,
        repo=_safe_repository_url(plan.source.repo_url),
        base_branch=plan.source.ref,
        path=plan.source.path,
        patch=plan.action.model_dump(mode="json"),
        diff=plan.file_plan.diff,
        message="Validated GitOps plan preview; no Git changes were made.",
    )


def _compatibility_pull_request(plan: Plan, pull_request: PullRequestRecord) -> ApplyPrResponse:
    return _compatibility_preview(plan).model_copy(
        update={
            "status": "pr_open",
            "head_branch": pull_request.head_branch,
            "pr_url": pull_request.pr_url,
            "message": "A pull request for this recommendation is already open.",
            "pull_request": pull_request,
        }
    )


def _compatibility_operation_response(
    plan: Plan,
    operation: Operation,
    recommendation_id: int,
    actor: str,
) -> dict[str, object]:
    return {
        "status": operation.status.value,
        "operation_id": operation.operation_id,
        "plan_id": plan.plan_id,
        "recommendation_id": recommendation_id,
        "idempotency_key": operation.idempotency_key,
        "fingerprint": plan.digest,
        "actor": actor,
        "policy_result": plan.policy_result,
    }


def _operation_response(
    operation: Operation,
    plan: Plan,
    pull_request: PullRequestRecord | None,
) -> dict[str, object]:
    return {
        "id": operation.operation_id,
        "operation_id": operation.operation_id,
        "plan_id": operation.plan_id,
        "recommendation_id": plan.recommendation_ids[0],
        "recommendation_ids": plan.recommendation_ids,
        "status": operation.status.value,
        "idempotency_key": operation.idempotency_key,
        "fingerprint": plan.digest,
        "preview_digest": plan.digest,
        "commit_digest": operation.commit_sha,
        "attempts": operation.attempts,
        "error": operation.last_error,
        "result": {"plan_status": plan.status.value},
        "pr_url": pull_request.pr_url if pull_request is not None else None,
        "policy_result": plan.policy_result,
    }


def _service_unavailable(
    detail: str = "Server-side automation plan service is not configured.",
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"code": "plan_service_unavailable", "detail": detail},
    )
