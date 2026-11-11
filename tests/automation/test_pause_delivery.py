"""automation.policy.pauseDelivery is the only off switch of always-on automation (F12)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_automation_plan_service
from greenkube.automation.controller import AutomationController, PlanExecutor
from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import AutomationErrorCode


async def test_paused_delivery_refuses_plan_approval_with_a_policy_error(monkeypatch):
    plans = SimpleNamespace(read_plan=AsyncMock())
    service = RuntimePlanService.__new__(RuntimePlanService)
    service._plan_service = cast(Any, plans)
    monkeypatch.setattr(
        "greenkube.automation.plan_runtime.get_config",
        lambda: SimpleNamespace(automation_delivery_paused=True),
    )

    with pytest.raises(AutomationPlanError) as raised:
        await service.approve_plan("plan-1", "sha256:" + "a" * 64, actor="user:admin")

    assert raised.value.code == AutomationErrorCode.DELIVERY_PAUSED
    plans.read_plan.assert_not_awaited()


def test_paused_approval_is_a_409_with_its_code():
    class PausedService:
        async def approve_plan(self, plan_id, digest, *, actor="system"):
            raise AutomationPlanError(AutomationErrorCode.DELIVERY_PAUSED, "paused")

    app = create_app()
    app.dependency_overrides[get_automation_plan_service] = lambda: PausedService()
    with TestClient(app) as client:
        response = client.post("/api/v1/automation/plans/plan-1/approve", json={"digest": "sha256:" + "a" * 64})
    app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["code"] == "delivery_paused"


async def test_plan_executor_waits_while_delivery_is_dormant():
    worker = SimpleNamespace(process_once=AsyncMock(return_value=False))
    active = {"value": False}
    executor = PlanExecutor(worker, active=lambda: active["value"])

    assert await executor.tick() == 0
    worker.process_once.assert_not_awaited()

    active["value"] = True
    await executor.tick()
    worker.process_once.assert_awaited_once()


def test_automation_controller_tracking_steps_follow_readiness():
    tracking = {"value": False}
    controller = AutomationController(
        plan_service=cast(Any, SimpleNamespace()),
        lease_repository=cast(Any, SimpleNamespace()),
        plan_worker=SimpleNamespace(),
        delivery_enabled=lambda: tracking["value"],
    )

    assert [step for step, _ in controller._steps()] == ["operations", "expiry", "retention"]
    tracking["value"] = True
    assert "webhooks" in [step for step, _ in controller._steps()]
