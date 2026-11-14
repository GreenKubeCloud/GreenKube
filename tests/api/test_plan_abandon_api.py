"""Plan API resilience: the busy check repeated at approval, and the operator abandon action.

POST /automation/plans/{id}/abandon fails a stuck APPLYING plan (operator or admin, audited).
"""

from __future__ import annotations

from typing import Any

from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import AutomationErrorCode, PlanStatus
from tests.api.auth_support import VIEWER_PASSWORD, auth_harness  # pyrefly: ignore[missing-import]
from tests.api.test_plans_router import _DIGEST, _plan, plan_api  # noqa: F401  # pyrefly: ignore[missing-import]


def test_approval_repeats_the_workload_busy_check(plan_api):  # noqa: F811
    """Another plan of the workload opened its pull request after this preview was created."""
    client, _, repository, _, queue = plan_api
    repository.plans["delivering"] = _plan(status=PlanStatus.PR_OPEN).model_copy(update={"plan_id": "delivering"})

    response = client.post("/api/v1/automation/plans/plan-1/approve", json={"digest": _DIGEST})

    assert response.status_code == 409
    assert response.json()["code"] == "workload_plan_already_exists"
    assert repository.plans["plan-1"].status is PlanStatus.VALIDATED
    assert queue.keys == []


def _install_abandon(api_service: Any, repository: Any, calls: list[tuple[str, str, str | None]]) -> None:
    async def abandon_plan(plan_id: str, *, actor: str, reason: str | None = None):
        calls.append((plan_id, actor, reason))
        plan = repository.plans.get(plan_id)
        if plan is None:
            return None
        if plan.status is not PlanStatus.APPLYING:
            raise AutomationPlanError(AutomationErrorCode.PLAN_STALE, "only an applying plan can be abandoned")
        repository.plans[plan_id] = plan.model_copy(update={"status": PlanStatus.FAILED})
        return repository.plans[plan_id]

    api_service.abandon_plan = abandon_plan


def test_abandon_fails_an_applying_plan_and_forwards_the_actor_and_reason(plan_api):  # noqa: F811
    client, _, repository, api_service, _ = plan_api
    calls: list[tuple[str, str, str | None]] = []
    _install_abandon(api_service, repository, calls)
    repository.plans["stuck"] = _plan(status=PlanStatus.APPLYING).model_copy(update={"plan_id": "stuck"})

    response = client.post("/api/v1/automation/plans/stuck/abandon", json={"reason": "provider migration"})

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert calls and calls[0][0] == "stuck" and calls[0][2] == "provider migration"


def test_abandon_conflicts_for_other_statuses_and_404_for_unknown_plans(plan_api):  # noqa: F811
    client, _, repository, api_service, _ = plan_api
    _install_abandon(api_service, repository, [])

    validated = client.post("/api/v1/automation/plans/plan-1/abandon")
    missing = client.post("/api/v1/automation/plans/missing/abandon")
    invalid = client.post("/api/v1/automation/plans/plan-1/abandon", json={"reason": "x" * 501})

    assert validated.status_code == 409
    assert validated.json()["code"] == "plan_stale"
    assert "applying" in validated.json()["detail"]
    assert missing.status_code == 404
    assert invalid.status_code == 422


def test_abandon_requires_the_approve_scope(tmp_path, monkeypatch):
    with auth_harness(tmp_path, monkeypatch) as h:
        h.login("viewer", VIEWER_PASSWORD)
        response = h.client.post("/api/v1/automation/plans/plan-1/abandon", json={}, headers=h.csrf())

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
