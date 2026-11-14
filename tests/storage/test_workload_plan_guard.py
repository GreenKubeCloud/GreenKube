"""One plan per workload: plan creation and approval are atomic per workload on SQLite and PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest

from greenkube.gitops.contracts import PlanStatus
from greenkube.storage.base_gitops_repository import (
    GitOpsProviderConfig,
    WorkloadPlanConflictError,
    WorkloadPlanGuard,
)
from tests.storage.test_gitops_repository import (  # noqa: F401  # pyrefly: ignore[missing-import]
    _insert_recommendations,
    _plan,
    _source,
    gitops_store,
)

_BUSY = frozenset({PlanStatus.APPLYING, PlanStatus.PR_OPEN, PlanStatus.MERGED, PlanStatus.SYNCED})
_REPLACEABLE = frozenset({PlanStatus.DRAFT, PlanStatus.VALIDATED, PlanStatus.APPROVED})
_CREATE_GUARD = WorkloadPlanGuard(blocking_statuses=_BUSY | _REPLACEABLE)


async def _ready(repository, manager, backend):
    await _insert_recommendations(manager, backend)
    await repository.save_provider_config(GitOpsProviderConfig(id="provider-github", provider_type="github"))
    return repository


@pytest.mark.asyncio
async def test_concurrent_plan_creation_for_one_workload_yields_one_plan(gitops_store):  # noqa: F811
    repository, manager, backend = gitops_store
    await _ready(repository, manager, backend)
    source = _source()
    candidates = [_plan(source, f"plan-race-{index}") for index in range(4)]

    results = await asyncio.gather(
        *(repository.save_plan_if_workload_idle(plan, _CREATE_GUARD, actor="test") for plan in candidates),
        return_exceptions=True,
    )

    saved = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, WorkloadPlanConflictError)]
    assert len(saved) == 1
    assert len(conflicts) == 3
    stored = await repository.list_plans_for_workload(
        cluster=source.cluster,
        namespace=source.namespace,
        workload_kind=source.workload_kind,
        workload_name=source.workload_name,
    )
    assert [plan.plan_id for plan in stored] == [saved[0].plan_id]


@pytest.mark.asyncio
async def test_guard_tolerates_previews_the_caller_supersedes_but_not_busy_plans(gitops_store):  # noqa: F811
    repository, manager, backend = gitops_store
    await _ready(repository, manager, backend)
    source = _source()
    preview = await repository.save_plan_if_absent(_plan(source, "plan-preview"), actor="test")
    replacing = WorkloadPlanGuard(
        blocking_statuses=_BUSY | _REPLACEABLE,
        tolerated_plan_ids=frozenset({preview.plan_id}),
        tolerated_statuses=_REPLACEABLE,
    )
    replacement = await repository.save_plan_if_workload_idle(_plan(source, "plan-replacement"), replacing)
    assert replacement.plan_id == "plan-replacement"
    # Re-saving an existing plan ID is idempotent and never refused.
    assert (await repository.save_plan_if_workload_idle(replacement, _CREATE_GUARD)).plan_id == "plan-replacement"

    await repository.set_plan_status(preview.plan_id, PlanStatus.APPLYING)
    with pytest.raises(WorkloadPlanConflictError) as raised:
        await repository.save_plan_if_workload_idle(_plan(source, "plan-third"), replacing)
    assert raised.value.plan_id == preview.plan_id and raised.value.status is PlanStatus.APPLYING
    assert await repository.get_plan("plan-third") is None

    # Another workload is never blocked.
    other = _plan(_source(source_id="source-worker", workload_name="worker"), "plan-worker")
    assert (await repository.save_plan_if_workload_idle(other, _CREATE_GUARD)).plan_id == "plan-worker"


@pytest.mark.asyncio
async def test_approval_transition_refuses_while_another_plan_of_the_workload_is_busy(gitops_store):  # noqa: F811
    repository, manager, backend = gitops_store
    await _ready(repository, manager, backend)
    source = _source()
    first = await repository.save_plan_if_absent(_plan(source, "plan-first"), actor="test")
    second = await repository.save_plan_if_absent(_plan(source, "plan-second"), actor="test")
    approve_guard = WorkloadPlanGuard(blocking_statuses=_BUSY | {PlanStatus.APPROVED})

    await repository.set_plan_status(first.plan_id, PlanStatus.PR_OPEN)
    with pytest.raises(WorkloadPlanConflictError):
        await repository.set_plan_status_if_workload_idle(
            second.plan_id,
            PlanStatus.APPROVED,
            approve_guard,
            expected_statuses=(PlanStatus.VALIDATED,),
            actor="user:alice",
        )
    assert (await repository.get_plan(second.plan_id)).status is PlanStatus.VALIDATED

    # A rollback may be approved while its original plan is still busy.
    rollback_guard = WorkloadPlanGuard(
        blocking_statuses=_BUSY | {PlanStatus.APPROVED},
        tolerated_plan_ids=frozenset({first.plan_id}),
        tolerated_statuses=_BUSY,
    )
    approved = await repository.set_plan_status_if_workload_idle(
        second.plan_id,
        PlanStatus.APPROVED,
        rollback_guard,
        expected_statuses=(PlanStatus.VALIDATED,),
        actor="user:alice",
    )
    assert approved is not None and approved.status is PlanStatus.APPROVED
    assert await repository.set_plan_status_if_workload_idle("missing", PlanStatus.APPROVED, approve_guard) is None


@pytest.mark.asyncio
async def test_concurrent_approvals_for_one_workload_approve_one_plan(gitops_store):  # noqa: F811
    repository, manager, backend = gitops_store
    await _ready(repository, manager, backend)
    source = _source()
    plans = [await repository.save_plan_if_absent(_plan(source, f"plan-approve-{index}")) for index in range(3)]
    guard = WorkloadPlanGuard(blocking_statuses=_BUSY | {PlanStatus.APPROVED})

    results = await asyncio.gather(
        *(
            repository.set_plan_status_if_workload_idle(
                plan.plan_id, PlanStatus.APPROVED, guard, expected_statuses=(PlanStatus.VALIDATED,)
            )
            for plan in plans
        ),
        return_exceptions=True,
    )

    approved = [result for result in results if not isinstance(result, BaseException) and result is not None]
    assert len(approved) == 1
    assert sum(isinstance(result, WorkloadPlanConflictError) for result in results) == 2
