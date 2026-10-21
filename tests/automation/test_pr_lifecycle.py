from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from greenkube.automation.git.base import PullRequestInfo
from greenkube.automation.pr_lifecycle import (
    PullRequestLifecycleOutcome,
    PullRequestLifecyclePlan,
    PullRequestLifecycleReconciler,
    PullRequestLifecycleRecord,
)
from greenkube.gitops.contracts import ActionStatus, PlanStatus, PullRequestEvent

PROVIDER_ID = "provider-github"
REPO = "acme/platform"
PLAN_ID = "plan-42"
PR_NUMBER = 42
MERGE_SHA = "a" * 40
MERGED_AT = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)
CLOSED_AT = datetime(2026, 10, 21, 12, 45, tzinfo=timezone.utc)


@dataclass
class MemoryPullRequest:
    id: int
    recommendation_id: int
    provider_id: str | None = PROVIDER_ID
    repo: str = REPO
    pr_number: int | None = PR_NUMBER
    plan_id: str | None = PLAN_ID
    status: str = "open"
    sync_status: str | None = None
    sync_error_code: str | None = None
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass
class MemoryPlan:
    plan_id: str
    recommendation_ids: Sequence[int]
    status: PlanStatus = PlanStatus.PR_OPEN
    digest: str | None = None


class MemoryLifecycleRepository:
    def __init__(
        self,
        *,
        records: list[MemoryPullRequest] | None = None,
        plan: MemoryPlan | None = None,
    ) -> None:
        self.records = (
            records
            if records is not None
            else [
                MemoryPullRequest(id=1, recommendation_id=1001),
                MemoryPullRequest(id=2, recommendation_id=1002),
            ]
        )
        self.plans = {PLAN_ID: plan or MemoryPlan(PLAN_ID, [1001, 1002])}
        self.action_statuses = {
            recommendation_id: ActionStatus.PR_OPEN for recommendation_id in self.plans[PLAN_ID].recommendation_ids
        }
        self.identity_lookups: list[tuple[str, str, int]] = []
        self.plan_lookups: list[str] = []
        self.pull_request_updates: list[tuple[int, dict[str, object]]] = []
        self.plan_updates: list[tuple[str, PlanStatus]] = []
        self.action_updates: list[tuple[list[int], ActionStatus]] = []

    async def get_pull_request_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> PullRequestLifecycleRecord | None:
        self.identity_lookups.append((provider_id, repo, pr_number))
        matches = [
            record
            for record in self.records
            if (record.provider_id, record.repo, record.pr_number) == (provider_id, repo, pr_number)
        ]
        return max(matches, key=lambda record: record.id) if matches else None

    async def list_pull_requests_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> list[PullRequestLifecycleRecord]:
        self.identity_lookups.append((provider_id, repo, pr_number))
        return [
            record
            for record in self.records
            if (record.provider_id, record.repo, record.pr_number) == (provider_id, repo, pr_number)
        ]

    async def get_pull_request_by_plan_id(self, plan_id: str) -> PullRequestLifecycleRecord | None:
        self.plan_lookups.append(plan_id)
        matches = [record for record in self.records if record.plan_id == plan_id]
        return max(matches, key=lambda record: record.id) if matches else None

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict[str, object],
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestLifecycleRecord:
        record = next(record for record in self.records if record.id == pr_id)
        self.pull_request_updates.append((pr_id, updates.copy()))
        for key, value in updates.items():
            setattr(record, key, value)
        return record

    async def get_plan(self, plan_id: str) -> PullRequestLifecyclePlan | None:
        return self.plans.get(plan_id)

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None:
        return self.action_statuses.get(recommendation_id)

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> PullRequestLifecyclePlan | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        self.plan_updates.append((plan_id, status))
        plan.status = status
        return plan

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int:
        ids = list(recommendation_ids)
        self.action_updates.append((ids, status))
        for recommendation_id in ids:
            self.action_statuses[recommendation_id] = status
        return len(ids)


class MemoryGitProvider:
    def __init__(self, current: PullRequestInfo | None) -> None:
        self.current = current
        self.calls: list[tuple[str, str, int]] = []

    async def get_pull_request(self, provider_id: str, repo: str, number: int) -> PullRequestInfo | None:
        self.calls.append((provider_id, repo, number))
        return self.current


def _reconciler(
    repository: MemoryLifecycleRepository,
    provider: MemoryGitProvider,
) -> PullRequestLifecycleReconciler:
    return PullRequestLifecycleReconciler(
        pull_request_repository=repository,
        plan_repository=repository,
        git_provider=provider,
    )


def _event(*, merged: bool, delivery_id: str = "delivery-1") -> PullRequestEvent:
    return PullRequestEvent(
        provider_id=PROVIDER_ID,
        delivery_id=delivery_id,
        repo=REPO,
        number=PR_NUMBER,
        action="closed",
        merged=merged,
        merge_commit_sha=MERGE_SHA if merged else None,
        occurred_at=MERGED_AT if merged else CLOSED_AT,
    )


def _provider_state(
    *,
    merged: bool,
    number: int = PR_NUMBER,
    merge_commit_sha: str | None = MERGE_SHA,
) -> PullRequestInfo:
    status = "closed"
    raw: dict[str, object] = {"number": number, "state": status, "merged": merged}
    if merged:
        raw.update({"merge_commit_sha": merge_commit_sha, "merged_at": MERGED_AT.isoformat()})
    else:
        raw.update({"merge_commit_sha": None, "closed_at": CLOSED_AT.isoformat()})
    return PullRequestInfo(
        number=number,
        url=f"https://github.com/{REPO}/pull/{number}",
        status=status,
        merged=merged,
        raw=raw,
    )


@pytest.mark.asyncio
async def test_reconciles_merge_from_current_provider_state_without_marking_applied():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MERGED
    assert result.plan_id == PLAN_ID
    assert repository.identity_lookups == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert repository.plan_lookups == [PLAN_ID]
    assert provider.calls == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert [record.status for record in repository.records] == ["merged", "merged"]
    assert [record.merge_commit_sha for record in repository.records] == [MERGE_SHA, MERGE_SHA]
    assert [record.merged_at for record in repository.records] == [MERGED_AT, MERGED_AT]
    assert repository.plan_updates == [(PLAN_ID, PlanStatus.MERGED)]
    assert repository.action_updates == [([1001, 1002], ActionStatus.MERGED)]
    assert repository.plans[PLAN_ID].status is not PlanStatus.APPLIED
    assert all(status is not ActionStatus.APPLIED for status in repository.action_statuses.values())
    from greenkube.api.metrics_endpoint import get_metrics_output

    assert "greenkube_pull_request_merge_detection_seconds_count" in get_metrics_output().decode()


@pytest.mark.asyncio
async def test_merged_pull_request_without_merge_sha_fails_sync_with_typed_state():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True, merge_commit_sha=None))
    reconciler = _reconciler(repository, provider)
    event = _event(merged=True).model_copy(update={"merge_commit_sha": None})

    result = await reconciler.reconcile(event, newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MERGED
    assert repository.plans[PLAN_ID].status is PlanStatus.SYNC_FAILED
    assert all(record.status == "merged" for record in repository.records)
    assert all(record.sync_status == "failed" for record in repository.records)
    assert all(record.sync_error_code == "merge_commit_sha_missing" for record in repository.records)
    assert all(status is ActionStatus.FAILED for status in repository.action_statuses.values())


@pytest.mark.asyncio
async def test_terminal_plan_retires_late_provider_merge_without_repolling():
    repository = MemoryLifecycleRepository()
    repository.plans[PLAN_ID].status = PlanStatus.FAILED
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.plans[PLAN_ID].status is PlanStatus.FAILED
    assert all(record.status == "merged" for record in repository.records)
    assert all(record.sync_status == "failed" for record in repository.records)
    assert all(record.sync_error_code == "terminal_plan_merged" for record in repository.records)
    assert repository.plan_updates == []


@pytest.mark.asyncio
async def test_reconciles_unmerged_close_and_persists_provider_close_time():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=False))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=False), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.CLOSED
    assert [record.status for record in repository.records] == ["closed", "closed"]
    assert [record.closed_at for record in repository.records] == [CLOSED_AT, CLOSED_AT]
    assert all(record.merge_commit_sha is None for record in repository.records)
    assert repository.plan_updates == [(PLAN_ID, PlanStatus.CLOSED)]
    assert repository.action_updates == [([1001, 1002], ActionStatus.CLOSED)]


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_lookup_or_reconcile_again():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=False)

    assert result.outcome is PullRequestLifecycleOutcome.DUPLICATE
    assert repository.identity_lookups == []
    assert provider.calls == []
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_ignores_non_closed_events_without_lookup():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)
    event = _event(merged=True).model_copy(update={"action": "synchronized"})

    result = await reconciler.reconcile(event, newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.IGNORED
    assert repository.identity_lookups == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_malformed_event_metadata_fails_closed_before_lookup():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)
    event = _event(merged=True).model_copy(update={"repo": f" {REPO}"})

    result = await reconciler.reconcile(event, newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.identity_lookups == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unlinked_event_does_not_fetch_or_update_provider_or_plan():
    repository = MemoryLifecycleRepository(records=[])
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.UNLINKED
    assert repository.identity_lookups == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert provider.calls == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_pr_record_without_plan_link_is_unlinked():
    repository = MemoryLifecycleRepository(records=[MemoryPullRequest(id=1, recommendation_id=1001, plan_id=None)])
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.UNLINKED
    assert provider.calls == []
    assert repository.plan_updates == []


@pytest.mark.asyncio
async def test_plan_lookup_must_match_exact_pr_reference_before_provider_fetch():
    repository = MemoryLifecycleRepository(
        records=[
            MemoryPullRequest(id=1, recommendation_id=1001),
            MemoryPullRequest(id=2, recommendation_id=1002, repo="acme/other", pr_number=43),
        ]
    )
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert provider.calls == []
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_provider_pr_number_mismatch_fails_closed_without_state_changes():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True, number=PR_NUMBER + 1))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_provider_repository_mismatch_fails_closed():
    repository = MemoryLifecycleRepository()
    current = _provider_state(merged=True)
    current.raw["base"] = {"repo": {"full_name": "acme/other"}}
    provider = MemoryGitProvider(current)
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_provider_payload_pr_number_mismatch_fails_closed():
    repository = MemoryLifecycleRepository()
    current = _provider_state(merged=True)
    current.raw["number"] = PR_NUMBER + 1
    provider = MemoryGitProvider(current)
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_payload_merge_claim_must_match_refetched_current_state():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=False))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_merge_commit_mismatch_fails_closed():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True, merge_commit_sha="b" * 40))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_refetched_open_state_cannot_confirm_a_closed_event():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(
        PullRequestInfo(
            number=PR_NUMBER,
            url=f"https://github.com/{REPO}/pull/{PR_NUMBER}",
            status="open",
            merged=False,
            raw={"number": PR_NUMBER, "state": "open", "merged": False},
        )
    )
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=False), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MISMATCHED
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []


@pytest.mark.asyncio
async def test_late_merge_event_preserves_applied_plan_and_action_statuses():
    repository = MemoryLifecycleRepository(plan=MemoryPlan(PLAN_ID, [1001, 1002], status=PlanStatus.APPLIED))
    repository.action_statuses = {
        1001: ActionStatus.APPLIED,
        1002: ActionStatus.APPLIED,
    }
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)

    assert result.outcome is PullRequestLifecycleOutcome.MERGED
    assert repository.plans[PLAN_ID].status is PlanStatus.APPLIED
    assert repository.action_statuses == {
        1001: ActionStatus.APPLIED,
        1002: ActionStatus.APPLIED,
    }
    assert repository.plan_updates == []
    assert repository.action_updates == []
    assert [record.status for record in repository.records] == ["merged", "merged"]


@pytest.mark.asyncio
async def test_merge_after_unmerged_close_upgrades_without_downgrade():
    repository = MemoryLifecycleRepository(
        records=[
            MemoryPullRequest(id=1, recommendation_id=1001, status="closed"),
            MemoryPullRequest(id=2, recommendation_id=1002, status="closed"),
        ],
        plan=MemoryPlan(PLAN_ID, [1001, 1002], status=PlanStatus.CLOSED),
    )
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile(
        _event(merged=True, delivery_id="delivery-after-close"),
        newly_deduplicated=True,
    )

    assert result.outcome is PullRequestLifecycleOutcome.MERGED
    assert repository.plans[PLAN_ID].status is PlanStatus.MERGED
    assert repository.action_statuses == {
        1001: ActionStatus.MERGED,
        1002: ActionStatus.MERGED,
    }
    assert [record.status for record in repository.records] == ["merged", "merged"]


@pytest.mark.asyncio
async def test_reconciling_the_same_newly_deduplicated_state_twice_is_idempotent():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    first = await reconciler.reconcile(_event(merged=True), newly_deduplicated=True)
    update_count = len(repository.pull_request_updates)
    plan_update_count = len(repository.plan_updates)
    action_update_count = len(repository.action_updates)
    second = await reconciler.reconcile(
        _event(merged=True, delivery_id="delivery-2"),
        newly_deduplicated=True,
    )

    assert first.outcome is second.outcome is PullRequestLifecycleOutcome.MERGED
    assert len(repository.pull_request_updates) == update_count
    assert len(repository.plan_updates) == plan_update_count
    assert len(repository.action_updates) == action_update_count


@pytest.mark.asyncio
async def test_poll_reconciliation_fetches_current_state_and_reconciles_merged_pr():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=True))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile_polled(PROVIDER_ID, REPO, PR_NUMBER)

    assert result.outcome is PullRequestLifecycleOutcome.MERGED
    assert result.plan_id == PLAN_ID
    assert provider.calls == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert [record.status for record in repository.records] == ["merged", "merged"]
    assert repository.plan_updates == [(PLAN_ID, PlanStatus.MERGED)]
    assert repository.action_updates == [([1001, 1002], ActionStatus.MERGED)]


@pytest.mark.asyncio
async def test_poll_reconciliation_closes_unmerged_pr_from_current_provider_state():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(_provider_state(merged=False))
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile_polled(PROVIDER_ID, REPO, PR_NUMBER)

    assert result.outcome is PullRequestLifecycleOutcome.CLOSED
    assert result.plan_id == PLAN_ID
    assert provider.calls == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert [record.status for record in repository.records] == ["closed", "closed"]
    assert repository.plan_updates == [(PLAN_ID, PlanStatus.CLOSED)]
    assert repository.action_updates == [([1001, 1002], ActionStatus.CLOSED)]


@pytest.mark.asyncio
async def test_poll_reconciliation_ignores_currently_open_pr_without_mutation():
    repository = MemoryLifecycleRepository()
    provider = MemoryGitProvider(
        PullRequestInfo(
            number=PR_NUMBER,
            url=f"https://github.com/{REPO}/pull/{PR_NUMBER}",
            status="open",
            merged=False,
            raw={"number": PR_NUMBER, "state": "open", "merged": False},
        )
    )
    reconciler = _reconciler(repository, provider)

    result = await reconciler.reconcile_polled(PROVIDER_ID, REPO, PR_NUMBER)

    assert result.outcome is PullRequestLifecycleOutcome.IGNORED
    assert provider.calls == [(PROVIDER_ID, REPO, PR_NUMBER)]
    assert repository.pull_request_updates == []
    assert repository.plan_updates == []
    assert repository.action_updates == []
