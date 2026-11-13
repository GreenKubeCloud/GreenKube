"""Delivery-mode routes of approved plans (roadmap F4).

* ``GET /automation/plans?status=approved&delivery=ci``: approved plans a CI job
  (or an operator) delivers, with their branch, base revision and PR text.
* ``GET /automation/plans/{id}/patch``: the approved plan as a ``git format-patch``.
* ``POST /automation/plans/{id}/delivery``: report the pull request opened
  outside GreenKube (``plans:deliver`` scope). GreenKube verifies it through the
  provider before tracking it; a repeated report is idempotent.

None of these routes writes to Git: ``patch`` and ``ci`` modes need no write
credentials in the cluster.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_authenticated_actor,
    get_automation_plan_service,
    require_scope,
)
from greenkube.api.routers.plans import _ERROR_RESPONSES, _error_response, _plan_response, _service_unavailable
from greenkube.api.schemas import (
    AutomationPlanDeliveryItem,
    AutomationPlanDeliveryPullRequest,
    AutomationPlanDeliveryRequest,
    AutomationPlanDeliveryResponse,
)
from greenkube.auth.principal import Scope
from greenkube.automation.delivery import PatchUnavailableError, build_plan_patch
from greenkube.gitops.contracts import Plan, PlanStatus, public_repository_url

router = APIRouter()

#: Plans whose approved patch can be downloaded (again, once a pull request is open).
_PATCHABLE_STATUSES = frozenset({PlanStatus.APPROVED, PlanStatus.APPLYING, PlanStatus.PR_OPEN})
_MAX_LISTED_PLANS = 100


def _worker(service: AutomationPlanApiService | None) -> Any:
    return getattr(service, "worker", None) if service is not None else None


@router.get(
    "/automation/plans",
    response_model=list[AutomationPlanDeliveryItem],
    responses=_ERROR_RESPONSES,
)
async def list_automation_plans(
    status: PlanStatus = Query(PlanStatus.APPROVED, description="Plan status to list."),
    delivery: Literal["direct", "patch", "ci"] | None = Query(
        None, description="Only plans approved for this delivery mode (F4)."
    ),
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> list[AutomationPlanDeliveryItem] | JSONResponse:
    """Plans by status and delivery mode; a CI job polls ``status=approved&delivery=ci``."""
    worker = _worker(service)
    list_plans = getattr(getattr(worker, "plans", None), "list_plans", None)
    if service is None or worker is None or not callable(list_plans):
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    plans = await cast(Any, list_plans)(status=status)
    items: list[AutomationPlanDeliveryItem] = []
    for plan in plans:
        if delivery is not None and (plan.delivery_mode or "direct") != delivery:
            continue
        items.append(await _delivery_item(worker, plan))
        if len(items) >= _MAX_LISTED_PLANS:
            break
    return items


@router.get(
    "/automation/plans/{plan_id}/patch",
    response_class=PlainTextResponse,
    response_model=None,
    responses={**_ERROR_RESPONSES, 200: {"content": {"text/x-patch": {}}, "description": "git format-patch mailbox"}},
)
async def download_automation_plan_patch(
    plan_id: str,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> PlainTextResponse | JSONResponse:
    """The approved plan as one ``git format-patch`` commit for ``git am --3way``."""
    worker = _worker(service)
    if service is None or worker is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    plan = await service.read_plan(plan_id)
    if plan is None:
        return _error_response(404, "plan_not_found", "Automation plan not found.")
    if plan.status not in _PATCHABLE_STATUSES:
        return _error_response(
            409,
            "plan_not_approved",
            "Only an approved plan can be downloaded as a patch; approve its digest first.",
        )
    from greenkube.automation.plan_worker import plan_branch_name

    content = await worker.external_delivery_content(plan)
    try:
        patch = build_plan_patch(
            plan,
            branch=plan_branch_name(plan),
            commit_message=content.commit_message,
            date=plan.created_at if plan.created_at.tzinfo else plan.created_at.replace(tzinfo=timezone.utc),
        )
    except PatchUnavailableError as error:
        return _error_response(409, "patch_unavailable", f"The plan cannot be served as a patch: {error}.")
    return PlainTextResponse(
        patch.text,
        media_type="text/x-patch",
        headers={
            "Content-Disposition": f'attachment; filename="{plan.plan_id}.patch"',
            "X-GreenKube-Branch": patch.branch,
            "X-GreenKube-Base-Branch": patch.base_ref,
            "X-GreenKube-Base-Revision": patch.base_revision,
            "X-GreenKube-Delivery-Mode": plan.delivery_mode or "direct",
        },
    )


@router.post(
    "/automation/plans/{plan_id}/delivery",
    dependencies=[Depends(require_scope(Scope.PLANS_DELIVER))],
    response_model=AutomationPlanDeliveryResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
async def report_automation_plan_delivery(
    plan_id: str,
    request: AutomationPlanDeliveryRequest,
    http_request: Request,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
) -> AutomationPlanDeliveryResponse | JSONResponse:
    """Track the pull request a CI job or an operator opened for an approved plan."""
    from greenkube.automation.plan_worker import ExternalDeliveryError

    worker = _worker(service)
    if service is None or worker is None:
        return _service_unavailable(
            "plan_service_unavailable", "Server-side automation plan service is not configured."
        )
    try:
        result = await worker.record_external_delivery(
            plan_id,
            pr_url=request.pr_url,
            head_sha=request.head_sha,
            actor=get_authenticated_actor(http_request),
        )
    except ExternalDeliveryError as error:
        return _error_response(error.status_code, error.code, str(error))
    response = AutomationPlanDeliveryResponse(
        plan=_plan_response(result.plan, await service.read_operation_for_plan(plan_id)),
        pull_request=AutomationPlanDeliveryPullRequest(
            number=result.pull_request_number,
            url=result.pull_request_url,
            head_sha=result.head_sha,
            linked_by=result.linked_by,
        ),
        created=result.created,
    )
    if not result.created:
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    return response


async def _delivery_item(worker: Any, plan: Plan) -> AutomationPlanDeliveryItem:
    from greenkube.automation.plan_worker import plan_branch_name

    title = body = None
    branch = None
    if plan.digest is not None:
        branch = plan_branch_name(plan)
        if plan.file_plan is not None:
            content = await worker.external_delivery_content(plan)
            title, body = content.title, content.body
    return AutomationPlanDeliveryItem(
        plan_id=plan.plan_id,
        status=plan.status,
        delivery_mode=plan.delivery_mode,
        digest=plan.digest,
        recommendation_ids=plan.recommendation_ids,
        repository=public_repository_url(plan.source.repo_url),
        base_branch=plan.source.ref,
        base_revision=plan.base_revision,
        branch=branch,
        patch_url=f"/api/v1/automation/plans/{plan.plan_id}/patch",
        delivery_url=f"/api/v1/automation/plans/{plan.plan_id}/delivery",
        title=title,
        body=body,
        created_at=plan.created_at if plan.created_at.tzinfo else plan.created_at.replace(tzinfo=timezone.utc),
        expires_at=plan.expires_at,
    )
