"""Create, inspect, approve and cancel digest-addressed automation plans."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_authenticated_actor,
    get_automation_plan_service,
)
from greenkube.api.schemas import (
    AutomationPlanApproveRequest,
    AutomationPlanCreateRequest,
    AutomationPlanErrorResponse,
    AutomationPlanFileResponse,
    AutomationPlanResponse,
    AutomationPlanSourceResponse,
    AutomationPlanValidationResponse,
)
from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import AutomationErrorCode, Operation, Plan, PlanStatus

router = APIRouter()

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": AutomationPlanErrorResponse, "description": "The requested plan does not exist."},
    409: {"model": AutomationPlanErrorResponse, "description": "The plan is stale or cannot transition."},
    422: {"model": AutomationPlanErrorResponse, "description": "The plan inputs are unsupported or ambiguous."},
    424: {"model": AutomationPlanErrorResponse, "description": "A required provider dependency is unavailable."},
    429: {"model": AutomationPlanErrorResponse, "description": "A workload automation policy budget is exhausted."},
    503: {"model": AutomationPlanErrorResponse, "description": "Server-side plan dependencies are not configured."},
}

_DOMAIN_ERRORS: dict[AutomationErrorCode, tuple[int, str]] = {
    AutomationErrorCode.PLAN_STALE: (409, "The plan is stale or cannot transition from its current state."),
    AutomationErrorCode.PLAN_EXPIRED: (409, "The plan has expired; create a new plan."),
    AutomationErrorCode.SOURCE_CHANGED: (409, "The source changed after the plan was created."),
    AutomationErrorCode.REVISION_CONFLICT: (409, "The source revision conflicts with the plan."),
    AutomationErrorCode.UNSUPPORTED_SOURCE: (422, "The resolved source is not supported for planning."),
    AutomationErrorCode.AMBIGUOUS_BINDING: (422, "The workload does not have one unambiguous resolved binding."),
    AutomationErrorCode.NEEDS_CHART_MAPPING: (422, "The workload requires a supported chart mapping before planning."),
    AutomationErrorCode.RENDER_VALIDATION_FAILED: (422, "The plan did not pass render validation."),
    AutomationErrorCode.REQUIRES_CONTAINER_TARGET: (422, "The action requires a supported container target."),
    AutomationErrorCode.UNSAFE_PATH: (422, "The plan contains an unsafe repository path."),
    AutomationErrorCode.PROVIDER_NOT_READY: (424, "A required Git provider dependency is not ready."),
    AutomationErrorCode.PROVIDER_CAPABILITY_MISSING: (424, "The configured Git provider lacks a required capability."),
    AutomationErrorCode.AUTOMATION_BUDGET_EXHAUSTED: (429, "The automation budget is exhausted."),
    AutomationErrorCode.POLICY_EVIDENCE_MISSING: (
        424,
        "Required policy evidence is unavailable; refresh recommendation or workload evidence and retry.",
    ),
    AutomationErrorCode.POLICY_RISK_EXCEEDED: (422, "The recommendation risk exceeds the configured maximum."),
    AutomationErrorCode.POLICY_CONFIDENCE_TOO_LOW: (
        422,
        "The recommendation confidence is below the configured minimum.",
    ),
    AutomationErrorCode.CHANGE_FREEZE_ACTIVE: (409, "A configured change freeze is active for this workload."),
    AutomationErrorCode.WORKLOAD_PR_LIMIT_EXCEEDED: (429, "The workload has reached its open pull-request limit."),
    AutomationErrorCode.WORKLOAD_COOLDOWN_ACTIVE: (429, "The workload is inside its configured change cooldown."),
    AutomationErrorCode.WORKLOAD_PLAN_ALREADY_EXISTS: (409, "Another plan for this workload is still active."),
    AutomationErrorCode.ROLLOUT_IN_PROGRESS: (409, "The workload has an in-progress rollout."),
    AutomationErrorCode.CONTROLLER_UNHEALTHY: (424, "The owning GitOps controller is not ready to sync."),
    AutomationErrorCode.POLICY_INTERACTION_CONFLICT: (
        422,
        "The workload has an unresolved HPA, VPA or PDB interaction.",
    ),
    AutomationErrorCode.ROLLBACK_NOT_READY: (409, "The linked recommendations are not ready for rollback."),
    AutomationErrorCode.ROLLBACK_UNSUPPORTED: (422, "This plan cannot be reversed safely."),
    AutomationErrorCode.ROLLBACK_CONFLICT: (409, "The original plan or current source changed."),
}
_CANCELLABLE_STATUSES = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED})


@router.post(
    "/automation/plans",
    response_model=AutomationPlanResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
async def create_automation_plan(
    request: AutomationPlanCreateRequest,
    http_request: Request,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanResponse | JSONResponse:
    """Prepare a plan from persisted recommendation actions and resolved sources."""
    if service is None:
        return _service_unavailable("plan_preparation_unavailable", "Server-side plan preparation is not configured.")
    try:
        if request.values_mapping is None:
            plan = await service.prepare_plan(
                request.recommendation_ids,
                actor=get_authenticated_actor(http_request),
            )
        else:
            plan = await service.prepare_plan(
                request.recommendation_ids,
                actor=get_authenticated_actor(http_request),
                values_mapping=request.values_mapping.model_dump(mode="python"),
            )
    except AutomationPlanError as error:
        return _domain_error_response(error)
    return _plan_response(plan)


@router.get(
    "/automation/plans/{plan_id}",
    response_model=AutomationPlanResponse,
    responses=_ERROR_RESPONSES,
)
async def get_automation_plan(
    plan_id: str,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanResponse | JSONResponse:
    """Return a secret-safe projection of one persisted plan."""
    if service is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    plan = await service.read_plan(plan_id)
    if plan is None:
        return _error_response(404, "plan_not_found", "Automation plan not found.")
    operation = await service.read_operation_for_plan(plan_id)
    return _plan_response(plan, operation)


@router.post(
    "/automation/plans/{plan_id}/rollback-preview",
    response_model=AutomationPlanResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
async def preview_automation_plan_rollback(
    plan_id: str,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanResponse | JSONResponse:
    """Build a persisted rollback preview from a synced plan under rollback review."""
    if service is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    try:
        plan = await service.prepare_rollback(plan_id)
    except AutomationPlanError as error:
        return _domain_error_response(error)
    return _plan_response(plan)


@router.post(
    "/automation/plans/{plan_id}/approve",
    response_model=AutomationPlanResponse,
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def approve_automation_plan(
    plan_id: str,
    request: AutomationPlanApproveRequest,
    http_request: Request,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanResponse | JSONResponse:
    """Approve the exact persisted digest and enqueue the plan idempotently."""
    if service is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    try:
        plan = await service.approve_plan(
            plan_id,
            request.digest,
            actor=get_authenticated_actor(http_request),
        )
    except AutomationPlanError as error:
        return _domain_error_response(error)
    operation = await service.read_operation_for_plan(plan_id)
    return _plan_response(plan, operation)


@router.post(
    "/automation/plans/{plan_id}/cancel",
    response_model=AutomationPlanResponse,
    responses=_ERROR_RESPONSES,
)
async def cancel_automation_plan(
    plan_id: str,
    http_request: Request,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanResponse | JSONResponse:
    """Invalidate a plan before it starts applying."""
    if service is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    plan = await service.read_plan(plan_id)
    if plan is None:
        return _error_response(404, "plan_not_found", "Automation plan not found.")
    if plan.status is PlanStatus.STALE:
        return _plan_response(plan)
    if plan.status not in _CANCELLABLE_STATUSES:
        return _error_response(409, AutomationErrorCode.PLAN_STALE.value, "Only non-applying plans can be cancelled.")
    try:
        cancelled = await service.mark_stale(
            plan_id,
            actor=get_authenticated_actor(http_request),
            reason_code="cancelled_by_user",
        )
    except AutomationPlanError as error:
        return _domain_error_response(error)
    if cancelled is None:
        return _error_response(404, "plan_not_found", "Automation plan not found.")
    return _plan_response(cancelled)


def _plan_response(plan: Plan, operation: Operation | None = None) -> AutomationPlanResponse:
    file_plan = plan.file_plan
    validation = file_plan.validation if file_plan is not None else None
    return AutomationPlanResponse(
        plan_id=plan.plan_id,
        recommendation_ids=plan.recommendation_ids,
        status=plan.status,
        digest=plan.digest,
        policy_result=plan.policy_result,
        operation_id=operation.operation_id if operation is not None else None,
        operation_status=operation.status if operation is not None else None,
        rollback_of_plan_id=plan.rollback_of_plan_id,
        source=AutomationPlanSourceResponse(
            controller=f"{plan.source.controller_kind.value}/{plan.source.controller_name}",
            repo=_safe_repository_url(plan.source.repo_url),
            base_branch=plan.source.ref,
            base_revision=file_plan.base_revision if file_plan is not None else None,
            path=plan.source.path,
            renderer=plan.source.renderer,
        ),
        files=[
            AutomationPlanFileResponse(
                path=edit.path,
                operation=edit.operation,
                original_sha=edit.original_sha,
            )
            for edit in file_plan.files
        ]
        if file_plan is not None
        else [],
        diff=file_plan.diff if file_plan is not None else None,
        rendered_diff=file_plan.rendered_diff if file_plan is not None else None,
        warnings=["render-warning"] * min(len(file_plan.warnings), 20) if file_plan is not None else [],
        warnings_truncated=bool(file_plan and len(file_plan.warnings) > 20),
        validation=(
            AutomationPlanValidationResponse(
                passed=validation.passed,
                target_changed=validation.target_changed,
                unrelated_changes=validation.unrelated_changes,
                requests_within_limits=validation.requests_within_limits,
                renderer=validation.renderer,
                output_digest=validation.output_digest,
            )
            if validation is not None
            else None
        ),
        created_at=plan.created_at,
        expires_at=plan.expires_at,
    )


def _safe_repository_url(value: str) -> str:
    if value == "unresolved":
        return value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "redacted"
    if parts.scheme.lower() not in {"http", "https", "ssh", "git"} or not host:
        return "redacted"
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))


def _domain_error_response(error: AutomationPlanError) -> JSONResponse:
    status_code, detail = _DOMAIN_ERRORS.get(
        error.code,
        (422, "The automation plan request could not be completed."),
    )
    mapping_options = error.mapping_options if error.code is AutomationErrorCode.NEEDS_CHART_MAPPING else None
    return _error_response(
        status_code,
        error.code.value,
        detail,
        policy_result=error.policy_result,
        mapping_options=mapping_options,
    )


def _service_unavailable(code: str, detail: str) -> JSONResponse:
    return _error_response(503, code, detail)


def _error_response(
    status_code: int,
    code: str,
    detail: str,
    *,
    policy_result: dict[str, Any] | None = None,
    mapping_options: dict[str, list[str]] | None = None,
) -> JSONResponse:
    body = AutomationPlanErrorResponse(
        code=code,
        detail=detail,
        policy_result=policy_result,
        mapping_options=mapping_options,
    ).model_dump(mode="json", exclude_none=True)
    return JSONResponse(status_code=status_code, content=body)
