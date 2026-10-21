import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from greenkube.automation.sync_runtime import (
    DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
    DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS,
    DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS,
    GitOpsSyncRuntime,
    parse_gitops_sync_poll_interval_seconds,
    parse_gitops_sync_timeout_seconds,
)
from greenkube.gitops.contracts import (
    ActionStatus,
    BindingStatus,
    ControllerKind,
    PlanStatus,
    RendererKind,
    WorkloadSource,
)
from greenkube.gitops.sync import GitOpsSyncReason
from greenkube.models.metrics import PullRequestRecord, PullRequestStatus

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MERGE_SHA = "a" * 40


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


def _plan():
    return SimpleNamespace(
        plan_id="plan-1",
        source=_source(),
        status=PlanStatus.MERGED,
        recommendation_ids=[41, 42],
        digest=None,
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
