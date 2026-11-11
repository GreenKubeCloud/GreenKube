"""Delivery-mode routes (roadmap F4): CI listing, patch download and delivery reports."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_automation_plan_service
from greenkube.automation.plan_worker import plan_branch_name
from greenkube.gitops.contracts import Plan, PlanStatus
from tests.automation.test_delivery_modes import (  # pyrefly: ignore[missing-import]
    HEAD_SHA,
    PR_URL,
    _external_worker,
)


class _Service:
    def __init__(self, worker: Any) -> None:
        self.worker = worker

    async def read_plan(self, plan_id: str) -> Plan | None:
        return await self.worker.plans.get_plan(plan_id)

    async def read_operation_for_plan(self, plan_id: str) -> None:
        return None


class _ListingPlans:
    """The fake plan repository with the ``list_plans`` the SQL repositories provide."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def list_plans(self, *, status: PlanStatus | None = None, **_: Any) -> list[Plan]:
        return [plan for plan in self.inner.plans.values() if status is None or plan.status is status]


@pytest.fixture
def delivery_client(monkeypatch):
    monkeypatch.setenv("GREENKUBE_API_KEY", "")
    monkeypatch.setenv("GREENKUBE_ENVIRONMENT", "development")

    def build(mode: str | None):
        worker, operations, plans, pull_requests, provider = _external_worker(mode)
        worker.plans = _ListingPlans(plans)
        app = create_app()
        app.dependency_overrides[get_automation_plan_service] = lambda: _Service(worker)
        return TestClient(app), plans, pull_requests, provider

    return build


def test_ci_job_lists_approved_ci_plans_with_branch_patch_and_pull_request_text(delivery_client):
    client, plans, _, _ = delivery_client("ci")
    plan = plans.plans["plan-1"]

    listed = client.get("/api/v1/automation/plans", params={"status": "approved", "delivery": "ci"})
    other_mode = client.get("/api/v1/automation/plans", params={"status": "approved", "delivery": "patch"})

    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    assert item["plan_id"] == "plan-1" and item["delivery_mode"] == "ci"
    assert item["branch"] == plan_branch_name(plan)
    assert item["base_branch"] == "main" and item["base_revision"] == plan.base_revision
    assert item["patch_url"] == "/api/v1/automation/plans/plan-1/patch"
    assert item["delivery_url"] == "/api/v1/automation/plans/plan-1/delivery"
    assert item["title"] and "GreenKube" in item["body"]
    assert "token" not in item["repository"]
    assert other_mode.json() == []


def test_patch_download_is_a_git_format_patch_with_its_branch_headers(delivery_client):
    client, plans, _, _ = delivery_client("patch")

    response = client.get("/api/v1/automation/plans/plan-1/patch")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/x-patch")
    assert response.headers["x-greenkube-branch"] == plan_branch_name(plans.plans["plan-1"])
    assert response.headers["x-greenkube-base-branch"] == "main"
    assert response.text.startswith("From ") and "GreenKube-Plan: plan-1" in response.text
    assert client.get("/api/v1/automation/plans/missing/patch").status_code == 404


def test_patch_download_needs_an_approved_plan(delivery_client):
    client, plans, _, _ = delivery_client("patch")
    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"status": PlanStatus.VALIDATED})

    response = client.get("/api/v1/automation/plans/plan-1/patch")

    assert response.status_code == 409
    assert response.json()["code"] == "plan_not_approved"


def test_ci_delivery_report_is_created_once_then_idempotent(delivery_client):
    client, plans, pull_requests, provider = delivery_client("ci")
    provider.open_pull_request(head_ref=plan_branch_name(plans.plans["plan-1"]))
    body = {"pr_url": PR_URL, "head_sha": HEAD_SHA}

    created = client.post("/api/v1/automation/plans/plan-1/delivery", json=body)
    repeated = client.post("/api/v1/automation/plans/plan-1/delivery", json=body)

    assert created.status_code == 201, created.text
    assert created.json()["plan"]["status"] == "pr_open"
    assert created.json()["plan"]["delivery_mode"] == "ci"
    assert created.json()["pull_request"] == {
        "number": 42,
        "url": PR_URL,
        "head_sha": HEAD_SHA,
        "linked_by": "branch",
    }
    assert repeated.status_code == 200 and repeated.json()["created"] is False
    assert len(pull_requests.records) == 2


def test_delivery_report_errors_are_typed(delivery_client):
    client, plans, _, provider = delivery_client("ci")
    provider.open_pull_request(head_ref="someone-else")

    unverified = client.post("/api/v1/automation/plans/plan-1/delivery", json={"pr_url": PR_URL, "head_sha": HEAD_SHA})
    malformed = client.post("/api/v1/automation/plans/plan-1/delivery", json={"pr_url": PR_URL, "head_sha": "abc"})
    missing = client.post("/api/v1/automation/plans/nope/delivery", json={"pr_url": PR_URL, "head_sha": HEAD_SHA})

    assert unverified.status_code == 409 and unverified.json()["code"] == "delivery_not_verified"
    assert malformed.status_code == 422
    assert missing.status_code == 404 and missing.json()["code"] == "plan_not_found"
    assert plans.plans["plan-1"].status is PlanStatus.APPROVED
