"""The expiry sweep reads only due plans in bounded batches (roadmap D15, BUG-57)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from greenkube.automation.plans import AutomationPlanService, GitBlobFile
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.gitops.contracts import ActionStatus, PlanStatus, ResourceTarget, WorkloadSource
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig
from greenkube.storage.sqlite.gitops_repository import SQLiteGitOpsRepository
from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
    DEPLOYMENT,
    NOW,
    FakeGitBlobResolver,
    FakePlanRevisionResolver,
    MemoryPlanRepository,
    _action,
    _archive_snapshot,
    _source,
)


@pytest.fixture
async def sqlite_plans(monkeypatch: pytest.MonkeyPatch):
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
    try:
        yield repository
    finally:
        await manager.close()


def _manifest(workload_name: str) -> str:
    return DEPLOYMENT.replace("  name: api\n  namespace", f"  name: {workload_name}\n  namespace")


class _PerWorkloadBlobResolver(FakeGitBlobResolver):
    async def get_blob_file(self, source: WorkloadSource, path: str, revision: str) -> GitBlobFile | None:
        self.lookups.append((source.provider_id, path, revision))
        return GitBlobFile(path=path, content=_manifest(source.workload_name), blob_sha=self.sha)


def _service(repository, now: list) -> AutomationPlanService:
    return AutomationPlanService(
        repository,
        git_blob_resolver=_PerWorkloadBlobResolver(),
        revision_resolver=FakePlanRevisionResolver(),
        clock=lambda: now[0],
        plan_ttl=timedelta(minutes=30),
    )


async def _create_preview(service, monkeypatch, workload_name: str, action_id: str):
    source = _source(id=f"source-{workload_name}", workload_name=workload_name, path=f"apps/{workload_name}")
    target = ResourceTarget(api_version="apps/v1", kind="Deployment", namespace="payments", name=workload_name)
    action = _action(action_id).model_copy(update={"target": target})
    snapshot = _archive_snapshot(monkeypatch, source, content=_manifest(workload_name))
    return await service.create_plan(action, source, snapshot)


@pytest.mark.asyncio
async def test_expiry_sweep_reads_only_due_plans_in_bounded_batches(sqlite_plans, monkeypatch):
    repository = sqlite_plans
    now = [NOW]
    service = _service(repository, now)
    first = await _create_preview(service, monkeypatch, "api", "plan-api")
    now[0] += timedelta(minutes=5)
    second = await _create_preview(service, monkeypatch, "web", "plan-web")
    now[0] += timedelta(minutes=10)
    fresh = await _create_preview(service, monkeypatch, "worker", "plan-worker")

    async def forbidden_list_plans(**_kwargs):
        raise AssertionError("the expiry sweep must not load every live plan")

    monkeypatch.setattr(repository, "list_plans", forbidden_list_plans)
    now[0] = NOW + timedelta(minutes=40)

    first_batch = await service.expire_due_plans(limit=1)
    second_batch = await service.expire_due_plans(limit=1)
    nothing_left = await service.expire_due_plans()

    assert [plan.plan_id for plan in first_batch] == [first.plan_id]
    assert [plan.plan_id for plan in second_batch] == [second.plan_id]
    assert nothing_left == []
    assert (await repository.get_plan(first.plan_id)).status is PlanStatus.STALE
    assert (await repository.get_plan(fresh.plan_id)).status is PlanStatus.VALIDATED
    with pytest.raises(ValueError):
        await service.expire_due_plans(limit=0)


@pytest.mark.asyncio
async def test_expiry_sweep_skips_a_plan_another_replica_already_moved(monkeypatch):
    repository = MemoryPlanRepository()
    now = [NOW]
    service = _service(repository, now)
    moved = await _create_preview(service, monkeypatch, "api", "plan-api")
    due = await _create_preview(service, monkeypatch, "web", "plan-web")
    stale_snapshot = dict(repository.plans)

    async def list_expired_plans(*, now, statuses, limit):
        # The sweep read both plans before another replica approved one of them.
        return [stale_snapshot[moved.plan_id], stale_snapshot[due.plan_id]][:limit]

    async def compare_and_set(plan_id, status, *, expected_statuses=None, expected_digest=None, **_kwargs):
        current = repository.plans[plan_id]
        if expected_statuses is not None and current.status not in expected_statuses:
            return None
        repository.plans[plan_id] = current.model_copy(update={"status": status})
        return repository.plans[plan_id]

    monkeypatch.setattr(repository, "list_expired_plans", list_expired_plans, raising=False)
    monkeypatch.setattr(repository, "set_plan_status", compare_and_set)
    repository.plans[moved.plan_id] = moved.model_copy(update={"status": PlanStatus.APPLYING})
    now[0] += timedelta(minutes=31)

    expired = await service.expire_due_plans()

    assert [plan.plan_id for plan in expired] == [due.plan_id]
    assert repository.plans[moved.plan_id].status is PlanStatus.APPLYING
    # The recommendation stays ready: the plan the other replica moved still carries it.
    assert repository.action_status[42] is ActionStatus.PLAN_READY
