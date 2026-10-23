import json
import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from greenkube.automation.branch_cleanup_runtime import BranchCleanupRuntime
from greenkube.automation.git.base import (
    GitProviderCapability,
    GitProviderTransientError,
    GitRepository,
    PullRequestInfo,
)
from greenkube.automation.plan_worker import GitProviderTarget, plan_branch_name
from greenkube.gitops.contracts import OperationStatus, Plan, PlanStatus
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.utils.log import configure_logging


def _read_json_logs(capfd):
    output = capfd.readouterr().out
    return output, [json.loads(line) for line in output.splitlines() if line]


def _plan(status: PlanStatus = PlanStatus.FAILED):
    source = SimpleNamespace(
        provider_id="gitea-main",
        repo_url="http://gitea.local/acme/manifests.git",
        ref="main",
    )
    return cast(
        Plan,
        SimpleNamespace(
            plan_id="plan-cleanup-1",
            digest="sha256:" + "a" * 64,
            recommendation_ids=(101, 102),
            source=source,
            status=status,
        ),
    )


def _records(plan, *, status=PullRequestStatus.ERROR, number=None, branch=None):
    head = branch or plan_branch_name(plan)
    return [
        PullRequestRecord(
            id=index,
            recommendation_id=recommendation_id,
            provider="gitea",
            provider_id="gitea-main",
            repo="acme/manifests",
            base_branch=plan.source.ref,
            head_branch=head,
            pr_number=number,
            plan_id=plan.plan_id,
            status=status,
        )
        for index, recommendation_id in enumerate(plan.recommendation_ids, start=1)
    ]


class MemoryCleanupRepository:
    def __init__(self, records, plans):
        self.records = records
        self.plans = plans
        self.completed: set[tuple[str, str, str, str]] = set()
        self.fail_mark_once = False

    async def list_branch_cleanup_candidates(self, *, limit, after_id=None):
        candidates = [
            record
            for record in self.records
            if record.plan_id
            and record.head_branch
            and (
                record.plan_id,
                record.provider_id,
                record.repo,
                record.head_branch,
            )
            not in self.completed
            and (
                record.status in {PullRequestStatus.CLOSED, PullRequestStatus.MERGED}
                or self.plans[record.plan_id].status
                in {
                    PlanStatus.MERGED,
                    PlanStatus.SYNCED,
                    PlanStatus.APPLIED,
                    PlanStatus.SYNC_FAILED,
                    PlanStatus.CLOSED,
                    PlanStatus.STALE,
                    PlanStatus.FAILED,
                    PlanStatus.NEEDS_MAPPING,
                    PlanStatus.AMBIGUOUS,
                    PlanStatus.REJECTED,
                    PlanStatus.SUPERSEDED,
                }
            )
            and (after_id is None or record.id > after_id)
        ]
        return candidates[:limit]

    async def list_pull_requests_by_plan_id(self, plan_id):
        return [record for record in self.records if record.plan_id == plan_id]

    async def mark_plan_branch_cleanup_complete(self, plan_id, *, provider_id, repo, branch_name):
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RuntimeError("simulated crash before cleanup completion was persisted")
        self.completed.add((plan_id, provider_id, repo, branch_name))


class MemoryPlanRepository:
    def __init__(self, plans):
        self.plans = plans

    async def get_plan(self, plan_id):
        return self.plans.get(plan_id)


class MemoryOperationRepository:
    def __init__(self, status=OperationStatus.FAILED):
        self.operation = SimpleNamespace(status=status)

    async def get_by_plan_id(self, plan_id):
        return self.operation


class FakeProvider:
    name = "gitea"
    capabilities = frozenset(
        {
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            GitProviderCapability.DELETE_PLAN_BRANCH,
        }
    )

    def __init__(self, branch):
        self.branch = branch
        self.default_branch = "main"
        self.current_pr = None
        self.open_pr = None
        self.delete_failures = []
        self.branch_exists = True
        self.calls = []

    def supports(self, capability):
        return capability in self.capabilities or getattr(capability, "value", capability) in {
            item.value for item in self.capabilities
        }

    async def get_default_branch(self, repo):
        self.calls.append(("default", repo.full_name))
        return self.default_branch

    async def get_pull_request(self, repo, number):
        self.calls.append(("pull_request", number))
        return self.current_pr

    async def find_open_pull_request_for_branch(self, repo, branch):
        self.calls.append(("open_branch", branch))
        return self.open_pr

    async def delete_plan_branch(self, repo, branch):
        self.calls.append(("delete", branch))
        if self.delete_failures:
            error = self.delete_failures.pop(0)
            raise error
        self.branch_exists = False

    async def close(self):
        self.calls.append(("close",))


class FakeProviderResolver:
    def __init__(self, provider):
        self.target = GitProviderTarget(
            provider=provider,
            repository=GitRepository(
                provider="gitea",
                owner="acme",
                name="manifests",
                default_branch="main",
            ),
        )

    async def resolve(self, source):
        return self.target


def _runtime(plan, records, provider, *, operation_status=OperationStatus.FAILED, repo=None):
    plans = {plan.plan_id: plan}
    pr_repository = repo or MemoryCleanupRepository(records, plans)
    runtime = BranchCleanupRuntime(
        pull_request_repository=pr_repository,
        plan_repository=MemoryPlanRepository(plans),
        operation_repository=MemoryOperationRepository(operation_status),
        provider_resolver=FakeProviderResolver(provider),
        batch_size=10,
    )
    return runtime, pr_repository


@pytest.mark.asyncio
async def test_terminal_plan_deletes_only_its_deterministic_branch_and_persists_completion():
    plan = _plan()
    branch = plan_branch_name(plan)
    records = _records(plan)
    provider = FakeProvider(branch)
    runtime, repository = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.selected == 1
    assert result.deleted == 1
    assert result.skipped == 0
    assert provider.calls.count(("delete", branch)) == 1
    assert repository.completed == {("plan-cleanup-1", "gitea-main", "acme/manifests", branch)}
    assert (await runtime.cleanup_once()).selected == 0
    assert provider.calls.count(("delete", branch)) == 1


@pytest.mark.asyncio
async def test_open_pr_for_head_blocks_deletion_even_when_the_plan_is_terminal():
    plan = _plan()
    branch = plan_branch_name(plan)
    records = _records(plan)
    provider = FakeProvider(branch)
    provider.open_pr = PullRequestInfo(
        number=77,
        url="https://gitea.local/acme/manifests/pulls/77",
        status="open",
        head_branch=branch,
        base_branch="release",
    )
    runtime, repository = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.deleted == 0
    assert not any(call[0] == "delete" for call in provider.calls)
    assert repository.completed == set()


@pytest.mark.asyncio
async def test_plan_base_branch_is_never_deleted():
    plan = _plan()
    branch = plan_branch_name(plan)
    plan.source.ref = branch
    records = _records(plan)
    provider = FakeProvider(branch)
    runtime, _ = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.skipped == 1
    assert provider.calls == [("close",)]


@pytest.mark.asyncio
async def test_closed_linked_pr_requires_current_provider_confirmation():
    plan = _plan(PlanStatus.PR_OPEN)
    branch = plan_branch_name(plan)
    records = _records(plan, status=PullRequestStatus.CLOSED, number=88)
    provider = FakeProvider(branch)
    provider.current_pr = PullRequestInfo(
        number=88,
        url="https://gitea.local/acme/manifests/pulls/88",
        status="open",
        head_branch=branch,
        base_branch="main",
    )
    runtime, _ = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.skipped == 1
    assert not any(call[0] == "delete" for call in provider.calls)


@pytest.mark.asyncio
async def test_closed_linked_pr_is_deleted_only_after_exact_terminal_provider_state():
    plan = _plan(PlanStatus.PR_OPEN)
    branch = plan_branch_name(plan)
    records = _records(plan, status=PullRequestStatus.CLOSED, number=88)
    provider = FakeProvider(branch)
    provider.current_pr = PullRequestInfo(
        number=88,
        url="https://gitea.local/acme/manifests/pulls/88",
        status="closed",
        head_branch=branch,
        base_branch="main",
    )
    runtime, repository = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.deleted == 1
    assert repository.completed == {("plan-cleanup-1", "gitea-main", "acme/manifests", branch)}


@pytest.mark.asyncio
async def test_merged_provider_state_must_be_consistent_before_cleanup():
    plan = _plan(PlanStatus.PR_OPEN)
    branch = plan_branch_name(plan)
    records = _records(plan, status=PullRequestStatus.MERGED, number=88)
    provider = FakeProvider(branch)
    provider.current_pr = PullRequestInfo(
        number=88,
        url="https://gitea.local/acme/manifests/pulls/88",
        status="merged",
        merged=False,
        head_branch=branch,
        base_branch="main",
    )
    runtime, repository = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()

    assert result.skipped == 1
    assert repository.completed == set()
    assert not any(call[0] == "delete" for call in provider.calls)


@pytest.mark.asyncio
async def test_unrelated_branch_name_and_active_operation_are_never_cleaned():
    plan = _plan()
    wrong_branch = "greenkube/plan/" + "b" * 32
    records = _records(plan, branch=wrong_branch)
    provider = FakeProvider(wrong_branch)
    runtime, _ = _runtime(plan, records, provider)

    result = await runtime.cleanup_once()
    assert result.skipped == 1
    assert not any(call[0] == "delete" for call in provider.calls)

    plan = _plan()
    records = _records(plan)
    provider = FakeProvider(plan_branch_name(plan))
    runtime, _ = _runtime(plan, records, provider, operation_status=OperationStatus.RUNNING)

    result = await runtime.cleanup_once()
    assert result.skipped == 1
    assert not any(call[0] == "delete" for call in provider.calls)


@pytest.mark.asyncio
async def test_transient_delete_failure_retries_and_recovers(capfd):
    configure_logging(level="INFO", log_format="json")
    plan = _plan()
    branch = plan_branch_name(plan)
    records = _records(plan)
    provider = FakeProvider(branch)
    provider.delete_failures.append(GitProviderTransientError("token=never-log-this"))
    runtime, repository = _runtime(plan, records, provider)

    first = await runtime.cleanup_once()
    second = await runtime.cleanup_once()

    assert first.errors == 1
    assert first.deleted == 0
    assert second.deleted == 1
    output, logs = _read_json_logs(capfd)
    failure = next(record for record in logs if record.get("failure_stage"))
    assert failure["failure_stage"] == "branch_delete"
    assert failure["failure_code"] == "provider_transient"
    assert failure["failure_type"] == "GitProviderTransientError"
    assert "token=never-log-this" not in output
    assert repository.completed == {("plan-cleanup-1", "gitea-main", "acme/manifests", branch)}


@pytest.mark.asyncio
async def test_candidate_query_recovers_after_disk_full_with_secret_free_diagnostics(capfd):
    configure_logging(level="INFO", log_format="json")
    plan = _plan()
    branch = plan_branch_name(plan)
    records = _records(plan)
    provider = FakeProvider(branch)

    class DiskFullOnceRepository(MemoryCleanupRepository):
        def __init__(self, linked_records, plans):
            super().__init__(linked_records, plans)
            self.fail_once = True

        async def list_branch_cleanup_candidates(self, *, limit, after_id=None):
            if self.fail_once:
                self.fail_once = False
                raise sqlite3.OperationalError("database or disk is full; token=never-log-this")
            return await super().list_branch_cleanup_candidates(limit=limit, after_id=after_id)

    repository = DiskFullOnceRepository(records, {plan.plan_id: plan})
    runtime, _ = _runtime(plan, records, provider, repo=repository)

    first = await runtime.cleanup_once()
    second = await runtime.cleanup_once()

    assert first.errors == 1
    output, logs = _read_json_logs(capfd)
    failure = next(record for record in logs if record.get("failure_stage"))
    assert failure["failure_stage"] == "candidate_query"
    assert failure["failure_code"] == "storage_capacity_exhausted"
    assert "token=never-log-this" not in output
    assert second.deleted == 1
    assert [call for call in provider.calls if call[0] == "delete"] == [("delete", branch)]


@pytest.mark.asyncio
async def test_remote_delete_before_database_completion_is_idempotently_recovered():
    plan = _plan()
    branch = plan_branch_name(plan)
    records = _records(plan)
    provider = FakeProvider(branch)
    repository = MemoryCleanupRepository(records, {plan.plan_id: plan})
    repository.fail_mark_once = True
    runtime, _ = _runtime(plan, records, provider, repo=repository)

    first = await runtime.cleanup_once()
    assert first.errors == 1
    assert repository.completed == set()

    second = await runtime.cleanup_once()

    assert second.deleted == 1
    assert provider.branch_exists is False
    assert [call for call in provider.calls if call[0] == "delete"] == [
        ("delete", branch),
        ("delete", branch),
    ]
    assert repository.completed == {("plan-cleanup-1", "gitea-main", "acme/manifests", branch)}
