"""Re-opening a preview supersedes it instead of returning 409 (roadmap D6, BUG-48)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError, AutomationPlanService
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.gitops.contracts import AutomationErrorCode, BindingStatus, PlanStatus, WorkloadSource
from greenkube.models.evidence import RecommendationEvidence
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, WorkloadBinding
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
    BASE_REVISION,
    NOW,
    FakeGitBlobResolver,
    FakePlanRevisionResolver,
    _action,
    _archive_snapshot,
    _source,
)


class _Recommendations:
    def __init__(self) -> None:
        self.record = RecommendationRecord(
            id=42,
            namespace="payments",
            owner_kind="Deployment",
            owner_name="api",
            scope="workload",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Right-size the api container.",
            action=_action(),
            status=RecommendationStatus.ACTIVE,
            risk_level="low",
            confidence=0.9,
            evidence=RecommendationEvidence(sample_count=48, coverage_ratio=1.0),
        )

    async def get_recommendation_by_id(self, rec_id: int) -> RecommendationRecord | None:
        return self.record if rec_id == 42 else None


class _Bindings:
    def __init__(self, source: WorkloadSource) -> None:
        self.source = source

    async def get_workload_binding(self, cluster: str, namespace: str, workload_kind: str, workload_name: str):
        return WorkloadBinding(
            cluster=cluster,
            namespace=namespace,
            workload_kind=workload_kind,
            workload_name=workload_name,
            status=BindingStatus.RESOLVED,
            confidence=1,
            source=self.source,
        )


class _Snapshots:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.commit_sha = BASE_REVISION

    async def create_snapshot(self, source: WorkloadSource):
        snapshot = _archive_snapshot(self.monkeypatch, source, commit_sha=self.commit_sha)
        # The helper's snapshot borrows the test workspace; closing it must not delete anything.
        return replace(snapshot, _workspace_owner=SimpleNamespace(cleanup=lambda: None))


@pytest.fixture
async def preview_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("DB_SCHEMA", "public")
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    async with manager.transaction() as connection:
        await connection.execute(
            "INSERT INTO recommendation_history (id, type, description, created_at, status) "
            "VALUES (42, 'rightsizing_cpu', 'Right-size', ?, 'active')",
            (NOW.isoformat(),),
        )
    repository = SQLiteGitOpsRepository(manager)
    await repository.save_provider_config(
        GitOpsProviderConfig(id="github-main", provider_type="github", status="ready")
    )
    now = [NOW]
    source = _source(observed_at=NOW, expires_at=NOW + timedelta(days=1))
    snapshots = _Snapshots(monkeypatch)
    plan_service = AutomationPlanService(
        repository,
        git_blob_resolver=FakeGitBlobResolver(),
        revision_resolver=FakePlanRevisionResolver(),
        clock=lambda: now[0],
    )
    runtime = RuntimePlanService(
        recommendation_repository=_Recommendations(),
        binding_repository=_Bindings(source),
        cluster="prod",
        snapshot_resolver=cast(Any, snapshots),
        plan_service=plan_service,
        policy_clock=lambda: now[0],
    )
    try:
        yield runtime, repository, snapshots, now
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_reopening_a_preview_returns_the_same_plan_instead_of_409(preview_runtime):
    runtime, repository, _, _ = preview_runtime

    first = await runtime.prepare_plan([42])
    second = await runtime.prepare_plan([42])

    assert first.status is PlanStatus.VALIDATED
    assert second.plan_id == first.plan_id
    assert second.status is PlanStatus.VALIDATED
    assert [plan.status for plan in await repository.list_plans()] == [PlanStatus.VALIDATED]


@pytest.mark.asyncio
async def test_preview_close_preview_returns_a_new_plan_and_supersedes_the_old_one(preview_runtime):
    """Acceptance for D6: preview -> close (DELETE) -> preview."""
    runtime, repository, _, _ = preview_runtime

    first = await runtime.prepare_plan([42])
    cancelled = await runtime.cancel_plan(first.plan_id, actor="tester")
    reopened = await runtime.prepare_plan([42])

    assert cancelled is not None and cancelled.status is PlanStatus.SUPERSEDED
    assert reopened.plan_id != first.plan_id
    assert reopened.status is PlanStatus.VALIDATED
    old = await repository.get_plan(first.plan_id)
    assert old is not None and old.status is PlanStatus.SUPERSEDED
    assert await repository.get_recommendation_action_status(42) is not None


@pytest.mark.asyncio
async def test_new_source_revision_preview_supersedes_the_open_preview(preview_runtime):
    runtime, repository, snapshots, _ = preview_runtime

    first = await runtime.prepare_plan([42])
    snapshots.commit_sha = "c" * 40
    second = await runtime.prepare_plan([42])

    assert second.plan_id != first.plan_id
    assert second.status is PlanStatus.VALIDATED
    old = await repository.get_plan(first.plan_id)
    assert old is not None and old.status is PlanStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_reopening_an_expired_preview_replans_instead_of_409(preview_runtime):
    runtime, repository, _, now = preview_runtime

    first = await runtime.prepare_plan([42])
    now[0] = NOW + timedelta(minutes=31)
    reopened = await runtime.prepare_plan([42])

    assert reopened.plan_id != first.plan_id
    assert reopened.status is PlanStatus.VALIDATED
    old = await repository.get_plan(first.plan_id)
    assert old is not None and old.status is PlanStatus.STALE


@pytest.mark.asyncio
async def test_approved_plans_still_block_a_new_preview(preview_runtime):
    runtime, repository, _, _ = preview_runtime

    first = await runtime.prepare_plan([42])
    assert await repository.set_plan_status(first.plan_id, PlanStatus.APPROVED) is not None

    with pytest.raises(AutomationPlanError) as error:
        await runtime.prepare_plan([42])

    assert error.value.code is AutomationErrorCode.WORKLOAD_PLAN_ALREADY_EXISTS
    with pytest.raises(AutomationPlanError) as cancel_error:
        await runtime.cancel_plan(first.plan_id)
    assert cancel_error.value.code is AutomationErrorCode.PLAN_STALE


@pytest.mark.asyncio
async def test_concurrent_create_never_overwrites_an_advanced_plan(preview_runtime, monkeypatch):
    """D12 / BUG-56: save_plan was a check-then-act upsert that could reset an approved plan."""
    runtime, repository, snapshots, _ = preview_runtime
    first = await runtime.prepare_plan([42])
    assert await repository.set_plan_status(first.plan_id, PlanStatus.APPROVED) is not None
    original_get_plan = repository.get_plan
    calls = {"count": 0}

    async def racing_get_plan(plan_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # the existence check ran before the concurrent insert committed
        return await original_get_plan(plan_id)

    monkeypatch.setattr(repository, "get_plan", racing_get_plan)
    result = await runtime._plan_service.create_plan(
        first.action,
        first.source,
        await snapshots.create_snapshot(first.source),
    )

    assert result.plan_id == first.plan_id
    assert result.status is PlanStatus.APPROVED
    stored = await original_get_plan(first.plan_id)
    assert stored is not None and stored.status is PlanStatus.APPROVED
