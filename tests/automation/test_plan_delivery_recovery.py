"""A plan whose operation is exhausted after its PENDING pull-request record was written is recovered.

Before, ``reconcile_exhausted_operations`` skipped any plan with a linked record, PR polling
ignores records without a number, and the workload busy check refused every new plan: the
plan stayed APPLYING forever (provider resilience, item 1).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.automation.git.base import GitProviderTransientError
from greenkube.automation.plan_runtime import RuntimePlanService
from greenkube.automation.plans import AutomationPlanError
from greenkube.gitops.contracts import ActionStatus, Operation, OperationStatus, Plan, PlanStatus
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from tests.automation.test_plan_worker import (  # pyrefly: ignore[missing-import]
    PLAN_DIGEST,
    FakePlanRepository,
    _worker,
)


class _Plans(FakePlanRepository):
    async def list_plans(self, *, status: PlanStatus | None = None, source_id: str | None = None) -> list[Plan]:
        return [plan for plan in self.plans.values() if status is None or plan.status is status]


class _Operations:
    def __init__(self, status: OperationStatus) -> None:
        self.operation = Operation(
            operation_id="operation-1",
            plan_id="plan-1",
            idempotency_key=f"plan:plan-1:{PLAN_DIGEST}",
            status=status,
            attempts=5,
        )
        self.cancelled: list[tuple[str, str]] = []

    async def get_by_plan_id(self, plan_id: str) -> Operation | None:
        return self.operation if plan_id == self.operation.plan_id else None

    async def cancel_unleased_operation(self, plan_id: str, reason: str) -> Operation | None:
        self.cancelled.append((plan_id, reason))
        if self.operation.status in {OperationStatus.QUEUED, OperationStatus.RETRYING, OperationStatus.FAILED}:
            self.operation = self.operation.model_copy(update={"status": OperationStatus.CANCELLED})
        return self.operation


def _crashed_after_pending(fail_mode: str):
    """Run the worker until the provider fails around PR creation, leaving PENDING records."""
    worker, _, plans, pull_requests, provider = _worker()
    recording = _Plans(plans.plans["plan-1"])
    worker.plans = recording
    provider.plan_repository = recording
    provider.pull_request_fail_mode = fail_mode

    return worker, recording, pull_requests, provider


async def _run_until_stranded(worker, plans: _Plans) -> None:
    assert await worker.process_once("worker-1")
    # The retry left the plan APPLYING; then the lease expired after the last attempt
    # (claim_next marks the operation failed without touching the plan).
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING


def _runtime(worker: Any, plans: _Plans) -> RuntimePlanService:
    plan_service = SimpleNamespace(
        repository=plans,
        read_plan=plans.get_plan,
    )
    return RuntimePlanService(
        recommendation_repository=cast(Any, object()),
        binding_repository=cast(Any, object()),
        cluster="prod",
        snapshot_resolver=cast(Any, object()),
        plan_service=cast(Any, plan_service),
        worker=cast(Any, worker),
    )


@pytest.mark.asyncio
async def test_exhausted_operation_adopts_the_pull_request_opened_before_the_crash():
    worker, plans, pull_requests, provider = _crashed_after_pending("after")
    await _run_until_stranded(worker, plans)
    assert [record.status for record in pull_requests.records] == [PullRequestStatus.PENDING] * 2
    assert all(record.pr_number is None for record in pull_requests.records)
    assert len(provider.pull_requests) == 1
    worker.operations = _Operations(OperationStatus.FAILED)
    runtime = _runtime(worker, plans)

    reconciled = await runtime.reconcile_exhausted_operations()

    assert [plan.status for plan in reconciled] == [PlanStatus.PR_OPEN]
    assert plans.plans["plan-1"].status is PlanStatus.PR_OPEN
    assert plans.reason_codes[-1] == "pull_request_adopted"
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.OPEN}
    assert {record.pr_number for record in pull_requests.records} == {17}
    assert set(plans.action_status.values()) == {ActionStatus.PR_OPEN}
    # Nothing was opened twice.
    assert len(provider.pull_request_creations) == 1


@pytest.mark.asyncio
async def test_exhausted_operation_without_a_pull_request_fails_the_plan_cleanly():
    worker, plans, pull_requests, provider = _crashed_after_pending("before")
    await _run_until_stranded(worker, plans)
    assert provider.pull_requests == {}
    worker.operations = _Operations(OperationStatus.FAILED)
    runtime = _runtime(worker, plans)

    reconciled = await runtime.reconcile_exhausted_operations()

    assert [plan.status for plan in reconciled] == [PlanStatus.FAILED]
    assert plans.reason_codes[-1] == "operation_retry_exhausted"
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.ERROR}
    assert all("no pull request was opened" in (record.error or "") for record in pull_requests.records)
    # The recommendations can be planned again.
    assert set(plans.action_status.values()) == {ActionStatus.FAILED}


@pytest.mark.asyncio
async def test_transient_provider_failure_during_recovery_retries_on_the_next_sweep():
    worker, plans, pull_requests, provider = _crashed_after_pending("after")
    await _run_until_stranded(worker, plans)
    worker.operations = _Operations(OperationStatus.FAILED)

    async def unavailable(*_args: Any) -> None:
        raise GitProviderTransientError("503")

    provider.find_pull_request = unavailable  # type: ignore[method-assign]
    runtime = _runtime(worker, plans)

    assert await runtime.reconcile_exhausted_operations() == []
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.PENDING}


@pytest.mark.asyncio
async def test_operator_abandons_a_stuck_applying_plan_with_an_audited_reason():
    worker, plans, pull_requests, _ = _crashed_after_pending("before")
    await _run_until_stranded(worker, plans)
    operations = _Operations(OperationStatus.RETRYING)
    worker.operations = operations
    runtime = _runtime(worker, plans)

    abandoned = await runtime.abandon_plan("plan-1", actor="user:alice", reason="provider migration")

    assert abandoned is not None and abandoned.status is PlanStatus.FAILED
    assert plans.reason_codes[-1] == "abandoned_by_operator"
    assert operations.operation.status is OperationStatus.CANCELLED
    assert operations.cancelled == [("plan-1", "abandoned by user:alice")]
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.ERROR}
    assert all("provider migration" in (record.error or "") for record in pull_requests.records)
    assert set(plans.action_status.values()) == {ActionStatus.FAILED}


@pytest.mark.asyncio
async def test_abandon_refuses_a_live_lease_a_recorded_pull_request_and_other_statuses():
    worker, plans, pull_requests, _ = _crashed_after_pending("before")
    await _run_until_stranded(worker, plans)
    worker.operations = _Operations(OperationStatus.RUNNING)
    runtime = _runtime(worker, plans)

    with pytest.raises(AutomationPlanError, match="lease"):
        await runtime.abandon_plan("plan-1", actor="user:alice")
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING

    first = pull_requests.records[0]
    pull_requests.records[0] = first.model_copy(update={"pr_number": 9, "status": PullRequestStatus.OPEN})
    worker.operations = _Operations(OperationStatus.FAILED)
    with pytest.raises(AutomationPlanError, match="#9"):
        await runtime.abandon_plan("plan-1", actor="user:alice")

    plans.plans["plan-1"] = plans.plans["plan-1"].model_copy(update={"status": PlanStatus.PR_OPEN})
    with pytest.raises(AutomationPlanError, match="only an applying plan"):
        await runtime.abandon_plan("plan-1", actor="user:alice")
    assert await runtime.abandon_plan("missing", actor="user:alice") is None


def test_pending_record_helper_ignores_records_with_numbers():
    from greenkube.automation.plan_runtime import _pending_without_number

    pending = PullRequestRecord(
        recommendation_id=1, provider="github", repo="acme/gitops", status=PullRequestStatus.PENDING
    )
    assert _pending_without_number(pending)
    assert not _pending_without_number(pending.model_copy(update={"pr_number": 3}))
    assert not _pending_without_number(pending.model_copy(update={"status": PullRequestStatus.OPEN}))
    assert not _pending_without_number(object())


@pytest.mark.asyncio
async def test_abandon_refuses_while_the_plan_branch_holds_an_untracked_open_pull_request():
    worker, plans, pull_requests, provider = _crashed_after_pending("after")
    await _run_until_stranded(worker, plans)
    assert len(provider.pull_requests) == 1
    worker.operations = _Operations(OperationStatus.FAILED)
    runtime = _runtime(worker, plans)

    with pytest.raises(AutomationPlanError, match="#17 is open on plan branch"):
        await runtime.abandon_plan("plan-1", actor="user:alice")

    assert plans.plans["plan-1"].status is PlanStatus.APPLYING
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.PENDING}


@pytest.mark.asyncio
async def test_abandon_refuses_while_the_provider_cannot_be_asked():
    worker, plans, _, provider = _crashed_after_pending("before")
    await _run_until_stranded(worker, plans)
    worker.operations = _Operations(OperationStatus.FAILED)

    async def unavailable(*_args: Any) -> None:
        raise GitProviderTransientError("503")

    provider.find_pull_request = unavailable  # type: ignore[method-assign]
    runtime = _runtime(worker, plans)

    with pytest.raises(AutomationPlanError, match="retry"):
        await runtime.abandon_plan("plan-1", actor="user:alice")
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING


@pytest.mark.asyncio
async def test_recovery_keeps_the_plan_when_the_provider_config_cannot_be_read():
    worker, plans, pull_requests, _ = _crashed_after_pending("after")
    await _run_until_stranded(worker, plans)
    worker.operations = _Operations(OperationStatus.FAILED)

    class _BrokenResolver:
        async def resolve(self, _source: Any) -> Any:
            raise OSError("database is locked")

    worker.provider_resolver = _BrokenResolver()
    runtime = _runtime(worker, plans)

    assert await runtime.reconcile_exhausted_operations() == []
    assert plans.plans["plan-1"].status is PlanStatus.APPLYING
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.PENDING}


@pytest.mark.asyncio
async def test_recovery_fails_the_plan_when_its_provider_is_gone():
    from greenkube.automation.plan_worker import PlanWorkerError

    worker, plans, pull_requests, _ = _crashed_after_pending("after")
    await _run_until_stranded(worker, plans)
    worker.operations = _Operations(OperationStatus.FAILED)

    class _GoneResolver:
        async def resolve(self, _source: Any) -> Any:
            raise PlanWorkerError("Git provider 'github' is not configured.")

    worker.provider_resolver = _GoneResolver()
    runtime = _runtime(worker, plans)

    reconciled = await runtime.reconcile_exhausted_operations()

    assert [plan.status for plan in reconciled] == [PlanStatus.FAILED]
    assert plans.reason_codes[-1] == "delivery_recovery_provider_unavailable"
    assert {record.status for record in pull_requests.records} == {PullRequestStatus.ERROR}
