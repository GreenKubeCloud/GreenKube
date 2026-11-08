import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import respx

from greenkube.automation.git.base import GitProviderTransientError, git_blob_id
from greenkube.automation.git.registry import GitProviderRegistry
from greenkube.automation.plan_worker import RegistryPlanProviderResolver
from greenkube.automation.sync_runtime import (
    DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
    DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS,
    DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS,
    GitOpsSyncRuntime,
    ProviderAncestryChecker,
    create_gitops_sync_runtime,
    parse_gitops_sync_poll_interval_seconds,
    parse_gitops_sync_timeout_seconds,
)
from greenkube.gitops.contracts import (
    ActionStatus,
    BindingStatus,
    ControllerKind,
    FileEdit,
    FilePlan,
    PlanStatus,
    RendererKind,
    ValidationResult,
    WorkloadSource,
)
from greenkube.gitops.sync import GitOpsSyncReason
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsProviderRepository
from tests.automation.fake_git_servers import (  # pyrefly: ignore[missing-import]
    FakeGiteaServer,
    FakeGitHubServer,
    FakeGitLabServer,
    FakeGitRepository,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MERGE_SHA = "a" * 40
BASE_SHA = "c" * 40
MANIFEST_PATH = "apps/payments/deployment.yaml"
ORIGINAL_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
  namespace: apps
spec:
  template:
    spec:
      containers:
        - name: payments
          image: registry.example/payments:1.0.0
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
"""
PLANNED_MANIFEST = ORIGINAL_MANIFEST.replace("cpu: 500m", "cpu: 300m")


def _file_plan(base_revision: str = BASE_SHA) -> FilePlan:
    return FilePlan(
        plan_id="plan-1",
        files=[
            FileEdit(
                path=MANIFEST_PATH,
                operation="update",
                original_sha=git_blob_id(ORIGINAL_MANIFEST),
                content=PLANNED_MANIFEST,
            )
        ],
        diff="diff",
        rendered_diff="rendered diff",
        base_revision=base_revision,
        renderer=RendererKind.RAW,
        validation=ValidationResult(
            target_changed=True,
            unrelated_changes=False,
            requests_within_limits=True,
            renderer=RendererKind.RAW,
            output_digest="sha256:" + "e" * 64,
        ),
        digest="sha256:" + "f" * 64,
    )


def _source() -> WorkloadSource:
    return WorkloadSource(
        id="source-1",
        cluster="prod",
        namespace="apps",
        workload_kind="Deployment",
        workload_name="payments",
        provider_id="provider-main",
        repo_url="https://github.com/acme/platform.git",
        ref="main",
        path="apps/payments",
        controller_kind=ControllerKind.ARGOCD,
        controller_name="payments",
        controller_namespace="argocd",
        renderer=RendererKind.RAW,
        confidence=0.95,
        status=BindingStatus.RESOLVED,
    )


def _argo_response(revision: str = MERGE_SHA) -> dict:
    return {
        "metadata": {},
        "items": [
            {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {"name": "payments", "namespace": "argocd"},
                "spec": {"source": {"repoURL": "https://github.com/acme/platform.git", "targetRevision": "main"}},
                "status": {
                    "sync": {"status": "Synced", "revision": revision},
                    "health": {"status": "Healthy"},
                },
            }
        ],
    }


class _CustomObjectsApi:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response if response is not None else _argo_response()
        self.calls = 0

    async def list_namespaced_custom_object(self, **kwargs):
        self.calls += 1
        return self.response


class _PullRequestRepository:
    def __init__(self, records: list[PullRequestRecord]) -> None:
        self.records = records
        self.queries: list[tuple[int, int | None]] = []
        self.updates: list[tuple[int, dict]] = []

    async def list_sync_pending_pull_requests(self, *, limit: int, after_id: int | None = None):
        self.queries.append((limit, after_id))
        records = [
            record
            for record in self.records
            if record.id is not None
            and (after_id is None or record.id > after_id)
            and record.status is PullRequestStatus.MERGED
            and record.plan_id
            and record.provider_id
            and record.merge_commit_sha
            and (record.sync_status not in {"synced", "failed"} or record.sync_error_code == "sync_timeout")
        ]
        return records[:limit]

    async def list_pull_requests_by_provider_reference(self, provider_id: str, repo: str, pr_number: int):
        return [
            record
            for record in self.records
            if record.provider_id == provider_id and record.repo == repo and record.pr_number == pr_number
        ]

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict[str, Any],
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ):
        record = next(record for record in self.records if record.id == pr_id)
        updated = record.model_copy(update=updates)
        self.records[self.records.index(record)] = updated
        self.updates.append((pr_id, updates.copy()))
        return updated


class _PlanRepository:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.action_statuses = {recommendation_id: ActionStatus.MERGED for recommendation_id in plan.recommendation_ids}
        self.plan_updates: list[PlanStatus] = []
        self.reason_codes: list[str | None] = []
        self.action_updates: list[tuple[list[int], ActionStatus]] = []

    async def get_plan(self, plan_id: str):
        return self.plan if self.plan.plan_id == plan_id else None

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Any | None:
        assert plan_id == self.plan.plan_id
        self.plan.status = status
        self.plan_updates.append(status)
        self.reason_codes.append(reason_code)
        return self.plan

    async def get_recommendation_action_status(self, recommendation_id: int):
        return self.action_statuses.get(recommendation_id)

    async def set_recommendation_action_status(
        self,
        recommendation_ids,
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ):
        ids = list(recommendation_ids)
        self.action_updates.append((ids, status))
        for recommendation_id in ids:
            self.action_statuses[recommendation_id] = status
        return len(ids)


def _plan(file_plan: FilePlan | None = None):
    return SimpleNamespace(
        plan_id="plan-1",
        source=_source(),
        status=PlanStatus.MERGED,
        recommendation_ids=[41, 42],
        digest=None,
        file_plan=file_plan or _file_plan(),
    )


def _pr_record(record_id: int, recommendation_id: int) -> PullRequestRecord:
    return PullRequestRecord(
        id=record_id,
        recommendation_id=recommendation_id,
        provider="github",
        provider_id="provider-main",
        repo="acme/platform",
        pr_number=40,
        plan_id="plan-1",
        status=PullRequestStatus.MERGED,
        merge_commit_sha=MERGE_SHA,
        merged_at=NOW - timedelta(seconds=10),
    )


@pytest.mark.asyncio
async def test_sync_runtime_marks_all_grouped_pr_records_and_actions_synced():
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    api = _CustomObjectsApi()
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        api,
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
    )

    summary = await runtime.poll_once()

    assert summary.synced == 1
    assert summary.errors == 0
    assert api.calls == 1
    assert plans.plan_updates == [PlanStatus.SYNCED]
    assert plans.action_updates == [([41, 42], ActionStatus.SYNC_PENDING)]
    assert all(record.sync_status == "synced" for record in pull_requests.records)
    assert all(record.sync_revision == MERGE_SHA for record in pull_requests.records)
    assert all(record.sync_observed_at == NOW for record in pull_requests.records)
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_gitops_sync_outcomes_total{outcome="synced"}' in output
    assert "greenkube_gitops_sync_lag_seconds_count" in output


@pytest.mark.asyncio
async def test_pending_sync_preserves_merged_plan_and_retries():
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response("b" * 40)),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
    )

    summary = await runtime.poll_once()

    assert summary.pending == 1
    assert plans.plan.status is PlanStatus.MERGED
    assert plans.action_updates == [([41, 42], ActionStatus.SYNC_PENDING)]
    assert all(record.sync_status == "pending" for record in pull_requests.records)
    assert all(record.sync_error_code == GitOpsSyncReason.REVISION_MISMATCH.value for record in pull_requests.records)


@pytest.mark.asyncio
async def test_sync_timeout_marks_plan_failed_without_marking_it_applied():
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response("b" * 40)),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=1,
    )

    summary = await runtime.poll_once()

    assert summary.failed == 1
    assert plans.plan.status is PlanStatus.SYNC_FAILED
    assert plans.action_updates == [([41, 42], ActionStatus.FAILED)]
    assert all(record.sync_status == "failed" for record in pull_requests.records)
    from greenkube.api.metrics_endpoint import get_metrics_output

    output = get_metrics_output().decode()
    assert 'greenkube_gitops_sync_outcomes_total{outcome="timeout"}' in output
    assert "greenkube_gitops_sync_timeouts_total" in output


@pytest.mark.asyncio
async def test_late_successful_sync_recovers_a_plan_failed_only_for_timeout():
    records = [_pr_record(1, 41).model_copy(update={"sync_status": "failed", "sync_error_code": "sync_timeout"})]
    plan = _plan()
    plan.recommendation_ids = [41]
    plans = _PlanRepository(plan)
    plans.plan.status = PlanStatus.SYNC_FAILED
    plans.action_statuses = {41: ActionStatus.FAILED}
    pull_requests = _PullRequestRepository(records)
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(MERGE_SHA)),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=1,
    )

    summary = await runtime.poll_once()

    assert summary.synced == 1
    assert plans.plan.status is PlanStatus.SYNCED
    assert plans.plan_updates == [PlanStatus.SYNCED]
    assert plans.action_updates == [([41], ActionStatus.SYNC_PENDING)]
    assert all(record.sync_status == "synced" for record in pull_requests.records)
    assert all(record.sync_error_code is None for record in pull_requests.records)


@pytest.mark.asyncio
async def test_timeout_retries_record_attempt_time_and_obey_backoff():
    record = _pr_record(1, 41).model_copy(
        update={
            "sync_status": "failed",
            "sync_error_code": "sync_timeout",
            "sync_observed_at": NOW - timedelta(seconds=DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS + 1),
        }
    )
    plan = _plan()
    plan.recommendation_ids = [41]
    plans = _PlanRepository(plan)
    plans.plan.status = PlanStatus.SYNC_FAILED
    plans.action_statuses = {41: ActionStatus.FAILED}
    pull_requests = _PullRequestRepository([record])
    api = _CustomObjectsApi(_argo_response("b" * 40))
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        api,
        clock=lambda: NOW,
        sync_retry_backoff_seconds=DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS,
    )

    first = await runtime.poll_once()
    second = await runtime.poll_once()

    assert first.pending == 1
    assert second.pending == 1
    assert api.calls == 1
    assert pull_requests.records[0].sync_status == "failed"
    assert pull_requests.records[0].sync_observed_at == NOW
    assert pull_requests.records[0].sync_error_code == "sync_timeout"


@pytest.mark.asyncio
async def test_sync_runtime_retries_provider_api_errors_without_marking_sync():
    pull_requests = _PullRequestRepository([_pr_record(1, 41)])
    plans = _PlanRepository(_plan())

    class _FailingApi:
        async def list_namespaced_custom_object(self, **kwargs):
            raise RuntimeError("private cluster detail")

    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _FailingApi(),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
    )

    summary = await runtime.poll_once()

    assert summary.errors == 1
    assert plans.plan.status is PlanStatus.MERGED
    assert plans.action_updates == []
    assert pull_requests.records[0].sync_status is None


@pytest.mark.asyncio
async def test_sync_loop_polls_immediately_and_cancels_cleanly():
    stop = asyncio.Event()
    pull_requests = _PullRequestRepository([])
    runtime = GitOpsSyncRuntime(
        pull_requests,
        _PlanRepository(_plan()),
        _CustomObjectsApi(),
        interval_seconds=30,
        sync_timeout_seconds=300,
    )

    async def stop_soon():
        await asyncio.sleep(0)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    await runtime.run(stop)
    await stopper

    assert pull_requests.queries == [(100, None)]


@pytest.mark.asyncio
async def test_sync_loop_does_not_poll_without_lease_leadership():
    stop = asyncio.Event()
    pull_requests = _PullRequestRepository([])
    runtime = GitOpsSyncRuntime(
        pull_requests,
        _PlanRepository(_plan()),
        _CustomObjectsApi(),
        interval_seconds=30,
        sync_timeout_seconds=300,
    )

    async def stop_soon():
        await asyncio.sleep(0)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    await runtime.run(stop, should_run=lambda: False)
    await stopper

    assert pull_requests.queries == []


def test_sync_interval_and_timeout_settings_are_bounded_seconds():
    assert parse_gitops_sync_poll_interval_seconds() == DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS
    assert parse_gitops_sync_poll_interval_seconds("60") == 60
    assert parse_gitops_sync_timeout_seconds() == DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS
    assert parse_gitops_sync_timeout_seconds("3600") == 3600

    for value in ("0", "9", "-1", "abc", "86401"):
        with pytest.raises(ValueError):
            parse_gitops_sync_poll_interval_seconds(value)
    for value in ("0", "59", "-1", "abc", "86401"):
        with pytest.raises(ValueError):
            parse_gitops_sync_timeout_seconds(value)


DESCENDANT_SHA = "d" * 40


class _AncestryChecker:
    def __init__(self, answer: bool | None, *, planned: bool | None = True) -> None:
        self.answer = answer
        self.planned = planned
        self.calls: list[tuple[str, str, str]] = []
        self.planned_calls: list[tuple[str, str, str]] = []

    async def is_ancestor(self, source: WorkloadSource, ancestor_sha: str, descendant_sha: str) -> bool | None:
        self.calls.append((source.provider_id, ancestor_sha, descendant_sha))
        return self.answer

    async def planned_changes_present(
        self, source: WorkloadSource, file_plan: FilePlan, observed_revision: str
    ) -> bool | None:
        self.planned_calls.append((source.provider_id, file_plan.plan_id, observed_revision))
        return self.planned


@pytest.mark.asyncio
async def test_bug_43_later_commit_before_controller_sync_still_confirms_the_merged_plan():
    """BUG-43: another commit landed after the merge; the controller deploys that descendant."""
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    checker = _AncestryChecker(True)
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(DESCENDANT_SHA)),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
        ancestry_checker=checker,
    )

    summary = await runtime.poll_once()

    assert summary.synced == 1
    assert checker.calls == [("provider-main", MERGE_SHA, DESCENDANT_SHA)]
    assert checker.planned_calls == [("provider-main", "plan-1", DESCENDANT_SHA)]
    assert plans.plan_updates == [PlanStatus.SYNCED]
    assert plans.reason_codes == [GitOpsSyncReason.SYNC_CONFIRMED_DESCENDANT.value]
    assert plans.action_updates == [([41, 42], ActionStatus.SYNC_PENDING)]
    assert all(record.sync_status == "synced" for record in pull_requests.records)
    assert all(record.sync_revision == DESCENDANT_SHA for record in pull_requests.records)


@pytest.mark.asyncio
async def test_exact_merge_revision_records_the_plain_confirmation_reason():
    plans = _PlanRepository(_plan())
    checker = _AncestryChecker(True)
    runtime = GitOpsSyncRuntime(
        _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)]),
        plans,
        _CustomObjectsApi(_argo_response(MERGE_SHA)),
        clock=lambda: NOW,
        ancestry_checker=checker,
    )

    assert (await runtime.poll_once()).synced == 1
    assert checker.calls == []
    assert plans.reason_codes == [GitOpsSyncReason.SYNC_CONFIRMED.value]


@pytest.mark.asyncio
async def test_non_descendant_controller_revision_stays_pending_with_a_clear_reason():
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(DESCENDANT_SHA)),
        clock=lambda: NOW,
        interval_seconds=30,
        sync_timeout_seconds=300,
        ancestry_checker=_AncestryChecker(False),
    )

    summary = await runtime.poll_once()

    assert summary.pending == 1
    assert plans.plan.status is PlanStatus.MERGED
    assert all(record.sync_status == "pending" for record in pull_requests.records)
    assert all(
        record.sync_error_code == GitOpsSyncReason.REVISION_NOT_DESCENDANT.value for record in pull_requests.records
    )


@pytest.mark.asyncio
async def test_plan_failed_by_timeout_because_of_a_later_commit_recovers_through_ancestry():
    records = [_pr_record(1, 41).model_copy(update={"sync_status": "failed", "sync_error_code": "sync_timeout"})]
    plan = _plan()
    plan.recommendation_ids = [41]
    plans = _PlanRepository(plan)
    plans.plan.status = PlanStatus.SYNC_FAILED
    plans.action_statuses = {41: ActionStatus.FAILED}
    pull_requests = _PullRequestRepository(records)
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(DESCENDANT_SHA)),
        clock=lambda: NOW,
        sync_timeout_seconds=60,
        ancestry_checker=_AncestryChecker(True),
    )

    assert (await runtime.poll_once()).synced == 1
    assert plans.plan.status is PlanStatus.SYNCED
    assert pull_requests.records[0].sync_revision == DESCENDANT_SHA


@pytest.mark.asyncio
async def test_descendant_without_the_planned_changes_stays_pending_until_the_sync_timeout():
    """D1: SYNCED is a busy status; a reverted plan must time out instead of blocking forever."""
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(_plan())
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(DESCENDANT_SHA)),
        clock=lambda: NOW,
        sync_timeout_seconds=300,
        ancestry_checker=_AncestryChecker(True, planned=False),
    )

    assert (await runtime.poll_once()).pending == 1
    assert plans.plan.status is PlanStatus.MERGED
    assert all(
        record.sync_error_code == GitOpsSyncReason.PLANNED_CHANGE_NOT_PRESENT.value for record in pull_requests.records
    )

    runtime.clock = lambda: NOW + timedelta(seconds=300)
    assert (await runtime.poll_once()).failed == 1
    assert plans.plan.status is PlanStatus.SYNC_FAILED
    assert plans.reason_codes == ["sync_timeout"]

    # The timeout retry sees the same superseded descendant and retires the
    # records, so they are no longer re-observed like a plain timeout.
    runtime.clock = lambda: NOW + timedelta(seconds=300 + DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS + 1)
    assert (await runtime.poll_once()).failed == 1
    assert plans.plan.status is PlanStatus.SYNC_FAILED
    assert all(record.sync_status == "failed" for record in pull_requests.records)
    assert all(
        record.sync_error_code == GitOpsSyncReason.PLANNED_CHANGE_NOT_PRESENT.value for record in pull_requests.records
    )
    assert (await runtime.poll_once()).selected == 0


@pytest.mark.asyncio
async def test_plan_without_its_file_plan_is_never_confirmed_on_a_descendant():
    plan = _plan()
    plan.file_plan = None
    pull_requests = _PullRequestRepository([_pr_record(1, 41), _pr_record(2, 42)])
    plans = _PlanRepository(plan)
    checker = _AncestryChecker(True)
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(DESCENDANT_SHA)),
        clock=lambda: NOW,
        sync_timeout_seconds=300,
        ancestry_checker=checker,
    )

    assert (await runtime.poll_once()).pending == 1
    assert plans.plan.status is PlanStatus.MERGED
    assert checker.planned_calls == []
    assert all(
        record.sync_error_code == GitOpsSyncReason.PLANNED_CHANGE_UNVERIFIED.value for record in pull_requests.records
    )


class _GitHubConfigs:
    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig:
        return GitOpsProviderConfig(id=provider_id, provider_type="github", secret_name="git", secret_key="token")

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return ["acme/*"]


class _GitHubSecrets:
    async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str:
        return "token"


def _github_checker() -> ProviderAncestryChecker:
    configs = _GitHubConfigs()
    registry = GitProviderRegistry(cast(GitOpsProviderRepository, configs), _GitHubSecrets())
    return ProviderAncestryChecker(RegistryPlanProviderResolver(configs, registry))


async def _observe_on_github(
    later_changes: dict[str, str | None],
    *,
    timed_out: bool = False,
    polls: int = 1,
) -> tuple[_PlanRepository, _PullRequestRepository, FakeGitHubServer]:
    """Merge the planned manifest, land ``later_changes`` on top, and observe the controller there."""
    repository = FakeGitRepository.with_files({MANIFEST_PATH: ORIGINAL_MANIFEST, "README.md": "docs\n"})
    base_sha = repository.refs["main"]
    merge_sha = repository.commit_on("main", {MANIFEST_PATH: PLANNED_MANIFEST}, "greenkube plan")
    later_sha = repository.commit_on("main", later_changes, "later commit")
    server = FakeGitHubServer(repository, owner="acme", name="platform")

    plan = _plan(_file_plan(base_sha))
    record_updates: dict[str, Any] = {"merge_commit_sha": merge_sha}
    if timed_out:
        plan.recommendation_ids = [41]
        record_updates.update(sync_status="failed", sync_error_code="sync_timeout")
    records = [_pr_record(1, 41).model_copy(update=record_updates)]
    if not timed_out:
        records.append(_pr_record(2, 42).model_copy(update=record_updates))
    plans = _PlanRepository(plan)
    if timed_out:
        plans.plan.status = PlanStatus.SYNC_FAILED
        plans.action_statuses = {41: ActionStatus.FAILED}
    pull_requests = _PullRequestRepository(records)
    clock = {"now": NOW}
    runtime = GitOpsSyncRuntime(
        pull_requests,
        plans,
        _CustomObjectsApi(_argo_response(later_sha)),
        clock=lambda: clock["now"],
        sync_timeout_seconds=300 if not timed_out else 60,
        ancestry_checker=_github_checker(),
    )
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        for _ in range(polls):
            await runtime.poll_once()
            clock["now"] += timedelta(seconds=DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS + 1)
    return plans, pull_requests, server


@pytest.mark.asyncio
async def test_descendant_that_only_changed_unrelated_files_confirms_sync_on_github():
    plans, pull_requests, server = await _observe_on_github({"README.md": "renovate bump\n"}, polls=2)

    assert plans.plan.status is PlanStatus.SYNCED
    assert plans.reason_codes == [GitOpsSyncReason.SYNC_CONFIRMED_DESCENDANT.value]
    assert all(record.sync_status == "synced" for record in pull_requests.records)
    # One compare call plus one recursive tree read; the planned file blob is unchanged.
    assert sorted(server.breakdown()) == [
        f"GET /repos/acme/platform/compare/{pull_requests.records[0].merge_commit_sha}"
        f"...{pull_requests.records[0].sync_revision}",
        f"GET /repos/acme/platform/git/trees/{pull_requests.records[0].sync_revision}",
    ]


@pytest.mark.asyncio
async def test_descendant_with_an_unrelated_edit_in_the_planned_file_confirms_sync_on_github():
    """Image write-back edits the planned manifest without touching the planned requests."""
    image_bump = PLANNED_MANIFEST.replace("payments:1.0.0", "payments:1.1.0")

    plans, pull_requests, server = await _observe_on_github({MANIFEST_PATH: image_bump})

    assert plans.plan.status is PlanStatus.SYNCED
    assert plans.reason_codes == [GitOpsSyncReason.SYNC_CONFIRMED_DESCENDANT.value]
    assert server.calls() == 4  # compare, tree, then the planned file at the observed and base revisions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later_content",
    [ORIGINAL_MANIFEST, PLANNED_MANIFEST.replace("cpu: 300m", "cpu: 250m"), None],
    ids=["reverted", "overwritten", "deleted"],
)
async def test_descendant_that_reverted_or_overwrote_the_planned_file_is_not_synced(later_content):
    """D1: a later revert, overwrite or deletion must never confirm the plan (it would stay busy forever)."""
    plans, pull_requests, _ = await _observe_on_github({MANIFEST_PATH: later_content})

    assert plans.plan.status is PlanStatus.MERGED
    assert plans.plan_updates == []
    assert all(record.sync_status == "pending" for record in pull_requests.records)
    assert all(
        record.sync_error_code == GitOpsSyncReason.PLANNED_CHANGE_NOT_PRESENT.value for record in pull_requests.records
    )


@pytest.mark.asyncio
async def test_timed_out_plan_whose_planned_file_was_later_overwritten_is_not_resurrected():
    """D1: re-observing historical SYNC_FAILED(sync_timeout) plans must not flip superseded ones to SYNCED."""
    overwritten = PLANNED_MANIFEST.replace("cpu: 300m", "cpu: 200m")

    plans, pull_requests, server = await _observe_on_github({MANIFEST_PATH: overwritten}, timed_out=True, polls=2)

    assert plans.plan.status is PlanStatus.SYNC_FAILED
    assert plans.plan_updates == []
    assert plans.action_statuses == {41: ActionStatus.FAILED}
    assert pull_requests.records[0].sync_status == "failed"
    # The overwrite is final: the record leaves the timeout-retry queue, so
    # the second poll neither selects it nor calls the provider again.
    assert pull_requests.records[0].sync_error_code == GitOpsSyncReason.PLANNED_CHANGE_NOT_PRESENT.value
    assert pull_requests.queries[-1] == (100, None)
    assert server.calls() == 4


_SERVERS = {
    "github": lambda repository: FakeGitHubServer(repository, owner="acme", name="gitops"),
    "gitlab": lambda repository: FakeGitLabServer(repository, project="acme/gitops"),
    "gitea": lambda repository: FakeGiteaServer(repository, owner="acme", name="gitops"),
}


class _ProviderConfigs:
    def __init__(self, provider: str) -> None:
        self.config = GitOpsProviderConfig(
            id=f"{provider}-main",
            provider_type=provider,
            api_base_url="http://gitea.local" if provider == "gitea" else None,
            secret_name="git",
            secret_key="token",
        )

    async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig | None:
        return self.config if provider_id == self.config.id else None

    async def list_provider_allowlists(self, provider_id: str) -> list[str]:
        return ["acme/*"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(_SERVERS))
async def test_planned_change_check_reads_only_planned_paths_on_every_provider(provider):
    files = {f"apps/team-{index}/deployment.yaml": f"kind: Deployment # {index}\n" for index in range(500)}
    files[MANIFEST_PATH] = ORIGINAL_MANIFEST
    repository = FakeGitRepository.with_files(files)
    base_sha = repository.refs["main"]
    merge_sha = repository.commit_on("main", {MANIFEST_PATH: PLANNED_MANIFEST})
    unrelated_sha = repository.commit_on("main", {"apps/team-1/deployment.yaml": "kind: Deployment # bump\n"})
    image_sha = repository.commit_on(
        "main", {MANIFEST_PATH: PLANNED_MANIFEST.replace("payments:1.0.0", "payments:2.0.0")}
    )
    reverted_sha = repository.commit_on("main", {MANIFEST_PATH: ORIGINAL_MANIFEST})
    server = _SERVERS[provider](repository)
    configs = _ProviderConfigs(provider)
    registry = GitProviderRegistry(cast(GitOpsProviderRepository, configs), _GitHubSecrets())
    checker = ProviderAncestryChecker(RegistryPlanProviderResolver(configs, registry))
    source = _source().model_copy(
        update={"provider_id": f"{provider}-main", "repo_url": f"https://{provider}.example/acme/gitops.git"}
    )
    file_plan = _file_plan(base_sha)

    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await checker.planned_changes_present(source, file_plan, merge_sha) is True
        assert await checker.planned_changes_present(source, file_plan, unrelated_sha) is True
        assert await checker.planned_changes_present(source, file_plan, image_sha) is True
        assert await checker.planned_changes_present(source, file_plan, reverted_sha) is False
        calls = server.calls()
        assert await checker.planned_changes_present(source, file_plan, reverted_sha) is False

    assert server.calls() == calls  # definitive answers are cached
    assert calls <= 4 * 3  # at most three reads per observed revision, whatever the repository size


@pytest.mark.asyncio
async def test_timed_out_plan_whose_planned_values_survived_recovers_on_github():
    plans, pull_requests, _ = await _observe_on_github({"README.md": "later\n"}, timed_out=True)

    assert plans.plan.status is PlanStatus.SYNCED
    assert pull_requests.records[0].sync_status == "synced"


class _AncestryProvider:
    name = "github"

    def __init__(self, answer: bool | None = True, *, supported: bool = True, error: Exception | None = None) -> None:
        self.answer = answer
        self.supported = supported
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.closed = 0

    def supports(self, capability) -> bool:
        return self.supported

    async def is_ancestor(self, repo, ancestor_sha: str, descendant_sha: str) -> bool | None:
        self.calls.append((ancestor_sha, descendant_sha))
        if self.error is not None:
            raise self.error
        return self.answer

    async def close(self) -> None:
        self.closed += 1


class _ProviderResolver:
    def __init__(self, provider: _AncestryProvider) -> None:
        self.provider = provider

    async def resolve(self, source: WorkloadSource) -> Any:
        from greenkube.automation.git.base import GitRepository

        repository = GitRepository(provider="github", owner="acme", name="platform", default_branch="main")
        return SimpleNamespace(provider=self.provider, repository=repository)


@pytest.mark.asyncio
async def test_provider_ancestry_checker_caches_definitive_answers_only():
    positive = _AncestryProvider(True)
    checker = ProviderAncestryChecker(cast(Any, _ProviderResolver(positive)))
    assert await checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is True
    assert await checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is True
    assert positive.calls == [(MERGE_SHA, DESCENDANT_SHA)]
    assert positive.closed == 1

    unknown = _AncestryProvider(None)
    checker = ProviderAncestryChecker(cast(Any, _ProviderResolver(unknown)))
    assert await checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is None
    assert await checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is None
    assert len(unknown.calls) == 2


@pytest.mark.asyncio
async def test_provider_ancestry_checker_fails_soft_on_errors_and_unsupported_adapters():
    failing = _AncestryProvider(error=GitProviderTransientError("rate limited"))
    failing_checker = ProviderAncestryChecker(cast(Any, _ProviderResolver(failing)))
    assert await failing_checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is None
    assert failing.closed == 1

    unsupported = _AncestryProvider(supported=False)
    unsupported_checker = ProviderAncestryChecker(cast(Any, _ProviderResolver(unsupported)))
    assert await unsupported_checker.is_ancestor(_source(), MERGE_SHA, DESCENDANT_SHA) is None
    assert await unsupported_checker.is_ancestor(_source(), "main", MERGE_SHA) is None
    assert unsupported.calls == []


@pytest.mark.asyncio
async def test_provider_ancestry_checker_uses_the_authorized_github_compare_api():
    repository = FakeGitRepository.with_files({"apps/payments/deployment.yaml": "kind: Deployment\n"})
    root_sha = repository.refs["main"]
    merge_sha = repository.commit_on("main", {"apps/payments/deployment.yaml": "kind: Deployment # merged\n"})
    later_sha = repository.commit_on("main", {"README.md": "renovate\n"})
    repository.refs["other"] = root_sha
    diverged_sha = repository.commit_on("other", {"README.md": "elsewhere\n"})
    server = FakeGitHubServer(repository, owner="acme", name="platform")

    class Configs:
        async def get_provider_config(self, provider_id: str) -> GitOpsProviderConfig:
            return GitOpsProviderConfig(id=provider_id, provider_type="github", secret_name="git", secret_key="token")

        async def list_provider_allowlists(self, provider_id: str) -> list[str]:
            return ["acme/*"]

    class Secrets:
        async def resolve_secret(self, *, namespace: str | None, name: str, key: str) -> str:
            return "token"

    configs = Configs()
    registry = GitProviderRegistry(cast(GitOpsProviderRepository, configs), Secrets())
    checker = ProviderAncestryChecker(RegistryPlanProviderResolver(configs, registry))
    with respx.mock(assert_all_called=False) as router:
        router.route(url__startswith=server.base_url).mock(side_effect=server.handle)
        assert await checker.is_ancestor(_source(), merge_sha, later_sha) is True
        assert await checker.is_ancestor(_source(), merge_sha, later_sha) is True
        assert await checker.is_ancestor(_source(), merge_sha, diverged_sha) is False

    assert server.calls() == 2
    from greenkube.api.metrics_endpoint import get_metrics_output

    assert 'greenkube_git_provider_operation_calls_count{operation="sync_ancestry"}' in get_metrics_output().decode()


def test_sync_runtime_factory_wires_provider_ancestry_for_provider_repositories():
    pull_requests = _PullRequestRepository([])
    provider_repository = MagicMock(spec=GitOpsProviderRepository)
    wired = create_gitops_sync_runtime(
        pull_requests, cast(Any, provider_repository), custom_objects_api=_CustomObjectsApi()
    )
    plain = create_gitops_sync_runtime(pull_requests, _PlanRepository(_plan()), custom_objects_api=_CustomObjectsApi())
    explicit = create_gitops_sync_runtime(
        pull_requests,
        _PlanRepository(_plan()),
        custom_objects_api=_CustomObjectsApi(),
        provider_resolver=cast(Any, _ProviderResolver(_AncestryProvider())),
    )

    assert isinstance(wired.ancestry_checker, ProviderAncestryChecker)
    assert plain.ancestry_checker is None
    assert isinstance(explicit.ancestry_checker, ProviderAncestryChecker)
