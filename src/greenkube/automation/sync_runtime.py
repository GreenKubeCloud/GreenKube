"""Bounded GitOps sync observation and plan lifecycle transitions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from greenkube.automation.git.base import (
    GitProviderCapability,
    GitProviderError,
    git_blob_id,
    is_full_commit_sha,
)
from greenkube.automation.git.budget import (
    DEFAULT_SYNC_ANCESTRY_PROVIDER_CALL_BUDGET,
    DEFAULT_SYNC_PLANNED_CHANGE_PROVIDER_CALL_BUDGET,
    provider_call_budget,
)
from greenkube.core.leader_election import DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS
from greenkube.core.observability import (
    record_automation_failure,
    record_sync_lag,
    record_sync_outcome,
    record_sync_timeout,
)
from greenkube.gitops.contracts import ActionStatus, FileEdit, FilePlan, Plan, PlanStatus, WorkloadSource
from greenkube.gitops.planned_values import planned_values_present
from greenkube.gitops.sha import GIT_OBJECT_SHA
from greenkube.gitops.sync import GitOpsSyncReason, GitOpsSyncStatus, observe_gitops_sync
from greenkube.models.metrics import PullRequestRecord
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

if TYPE_CHECKING:
    from greenkube.automation.plan_worker import PlanProviderResolver

DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 30
MIN_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 10
MAX_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 86_400
DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS = 1800
MIN_GITOPS_SYNC_TIMEOUT_SECONDS = 60
MAX_GITOPS_SYNC_TIMEOUT_SECONDS = 86_400
DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS = 30
GITOPS_SYNC_POLL_INTERVAL_ENV = "GITOPS_SYNC_POLL_INTERVAL"
GITOPS_SYNC_TIMEOUT_ENV = "GITOPS_SYNC_TIMEOUT"
DEFAULT_ANCESTRY_CACHE_SIZE = 1024
DEFAULT_FEEDBACK_INTERVAL_SECONDS = 300

logger = logging.getLogger(__name__)


class GitAncestryChecker(Protocol):
    """Answer the two Git questions that confirm a sync on a descendant revision (D1)."""

    async def is_ancestor(self, source: WorkloadSource, ancestor_sha: str, descendant_sha: str) -> bool | None:
        """Return whether ``descendant_sha`` contains ``ancestor_sha`` (``None``: unknown)."""
        ...

    async def planned_changes_present(
        self,
        source: WorkloadSource,
        file_plan: FilePlan,
        observed_revision: str,
    ) -> bool | None:
        """Return whether ``observed_revision`` still carries the plan's file changes (``None``: unknown)."""
        ...


class ProviderAncestryChecker:
    """Ask the workload's authorized Git provider about descendant revisions (D1).

    Ancestry between two immutable commits, and the content of planned files
    at an immutable commit, never change, so definitive answers (positive and
    negative) are cached; ``None`` (unknown commit, provider error, exhausted
    budget, unsupported adapter) is never cached and is retried next poll.
    """

    def __init__(
        self,
        provider_resolver: PlanProviderResolver,
        *,
        cache_size: int = DEFAULT_ANCESTRY_CACHE_SIZE,
        call_budget: int = DEFAULT_SYNC_ANCESTRY_PROVIDER_CALL_BUDGET,
        planned_change_call_budget: int = DEFAULT_SYNC_PLANNED_CHANGE_PROVIDER_CALL_BUDGET,
    ) -> None:
        if type(cache_size) is not int or cache_size <= 0:
            raise ValueError("cache_size must be a positive integer.")
        self.provider_resolver = provider_resolver
        self.cache_size = cache_size
        self.call_budget = call_budget
        self.planned_change_call_budget = planned_change_call_budget
        self._cache: OrderedDict[Hashable, bool] = OrderedDict()

    async def is_ancestor(self, source: WorkloadSource, ancestor_sha: str, descendant_sha: str) -> bool | None:
        if GIT_OBJECT_SHA.fullmatch(ancestor_sha) is None or GIT_OBJECT_SHA.fullmatch(descendant_sha) is None:
            return None
        ancestor, descendant = ancestor_sha.lower(), descendant_sha.lower()
        key = ("ancestry", source.provider_id, source.repo_url, ancestor, descendant)
        return await self._cached(
            key,
            "sync_ancestry",
            self.call_budget,
            partial(self._ask_provider, source, ancestor, descendant),
        )

    async def planned_changes_present(
        self,
        source: WorkloadSource,
        file_plan: FilePlan,
        observed_revision: str,
    ) -> bool | None:
        """Check the planned paths at ``observed_revision`` against the plan's edits.

        A path whose blob equals the planned content is present. A deleted path
        must stay absent. When a later commit edited a planned file, that file
        is read at the observed and base revisions and compared structurally
        (:func:`planned_values_present`), so unrelated edits in the same file
        are accepted while reverts and overwrites are not. Costs one tree read
        plus two file reads per planned file that a later commit edited.
        """
        if not isinstance(file_plan, FilePlan) or not file_plan.files:
            return None
        if not is_full_commit_sha(observed_revision) or not is_full_commit_sha(file_plan.base_revision):
            return None
        observed = observed_revision.lower()
        key = ("planned", source.provider_id, source.repo_url, file_plan.plan_id, file_plan.digest, observed)
        return await self._cached(
            key,
            "sync_planned_change",
            self.planned_change_call_budget,
            partial(self._check_planned_changes, source, file_plan, observed),
        )

    async def _cached(
        self,
        key: Hashable,
        operation: str,
        call_budget: int,
        ask: Callable[[], Any],
    ) -> bool | None:
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        try:
            with provider_call_budget(operation, limit=call_budget):
                answer = await ask()
        except (GitProviderError, RuntimeError) as error:
            logger.warning("Git %s lookup failed for a merged plan (%s).", operation, type(error).__name__)
            return None
        if not isinstance(answer, bool):
            return None
        self._cache[key] = answer
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return answer

    async def _ask_provider(self, source: WorkloadSource, ancestor_sha: str, descendant_sha: str) -> bool | None:
        target = await self.provider_resolver.resolve(source)
        provider: Any = target.provider
        try:
            if not provider.supports(GitProviderCapability.COMMIT_ANCESTRY):
                return None
            answer = await provider.is_ancestor(target.repository, ancestor_sha, descendant_sha)
            return answer if isinstance(answer, bool) else None
        finally:
            await _close_quietly(provider)

    async def _check_planned_changes(self, source: WorkloadSource, file_plan: FilePlan, observed: str) -> bool | None:
        target = await self.provider_resolver.resolve(source)
        provider: Any = target.provider
        repository = target.repository
        try:
            blob_ids = await provider.get_blob_ids(repository, observed, [edit.path for edit in file_plan.files])
            if not isinstance(blob_ids, Mapping):
                return None
            object_format = "sha256" if len(observed) == 64 else "sha1"
            for edit in file_plan.files:
                blob = blob_ids.get(edit.path)
                if blob is not None and not isinstance(blob, str):
                    return None
                if edit.operation == "delete":
                    if blob is not None:
                        return False
                    continue
                if blob is None:
                    return False
                assert edit.content is not None
                if blob.lower() == git_blob_id(edit.content, object_format=object_format):
                    continue
                present = await _planned_file_present(provider, repository, file_plan.base_revision, edit, observed)
                if present is not True:
                    return present
            return True
        finally:
            await _close_quietly(provider)


async def _planned_file_present(
    provider: Any,
    repository: Any,
    base_revision: str,
    edit: FileEdit,
    observed: str,
) -> bool | None:
    """Compare one planned file that a later commit edited, structurally."""
    observed_file = await provider.get_file(repository, edit.path, observed)
    if observed_file is None or not isinstance(observed_file.content, str):
        return None
    original: str | None = None
    if edit.operation == "update":
        base_file = await provider.get_file(repository, edit.path, base_revision)
        if base_file is None or not isinstance(base_file.content, str):
            return None
        object_format = "sha256" if len(base_revision) == 64 else "sha1"
        base_blob = base_file.blob_sha or git_blob_id(base_file.content, object_format=object_format)
        if edit.original_sha is None or base_blob.lower() != edit.original_sha.lower():
            # Not the content the plan was computed from: the diff cannot be trusted.
            return False
        original = base_file.content
    assert edit.content is not None
    return planned_values_present(original, edit.content, observed_file.content)


async def _close_quietly(provider: Any) -> None:
    try:
        await provider.close()
    except Exception:
        pass


class SyncPullRequestRepository(Protocol):
    """Bounded source of merged pull requests awaiting sync observation."""

    async def list_sync_pending_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> list[PullRequestRecord]: ...

    async def list_pull_requests_by_provider_reference(
        self,
        provider_id: str,
        repo: str,
        pr_number: int,
    ) -> list[PullRequestRecord]: ...

    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict[str, Any],
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestRecord: ...


class SyncPlanRepository(Protocol):
    """Plan/action state mutations needed after a controller confirms sync."""

    async def get_plan(self, plan_id: str) -> Plan | None: ...

    async def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        expected_statuses: Sequence[PlanStatus] | None = None,
        expected_digest: str | None = None,
        actor: str = "system",
        reason_code: str | None = None,
    ) -> Plan | None: ...

    async def get_recommendation_action_status(self, recommendation_id: int) -> ActionStatus | None: ...

    async def set_recommendation_action_status(
        self,
        recommendation_ids: Sequence[int],
        status: ActionStatus,
        *,
        expected_statuses: Sequence[ActionStatus] | None = None,
        actor: str = "system",
        plan_id: str | None = None,
    ) -> int: ...


class PullRequestFeedback(Protocol):
    """Lifecycle-comment publisher notified on sync transitions (J5, ``automation.pr_feedback``)."""

    async def notify(self, plan_id: str) -> None: ...

    async def reconcile_once(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class GitOpsSyncCycleSummary:
    """Secret-free result counts for one bounded sync-observation cycle."""

    selected: int = 0
    observed: int = 0
    synced: int = 0
    pending: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0


class GitOpsSyncRuntime:
    """Observe merged PRs and transition plans only after verified controller sync."""

    def __init__(
        self,
        pull_request_repository: SyncPullRequestRepository,
        plan_repository: SyncPlanRepository,
        custom_objects_api: Any,
        *,
        batch_size: int = MAX_PULL_REQUEST_POLL_BATCH_SIZE,
        interval_seconds: int | float = DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
        sync_timeout_seconds: int = DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS,
        sync_retry_backoff_seconds: int = DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS,
        clock: Callable[[], datetime] | None = None,
        ancestry_checker: GitAncestryChecker | None = None,
        feedback: PullRequestFeedback | None = None,
        feedback_interval_seconds: int = DEFAULT_FEEDBACK_INTERVAL_SECONDS,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_PULL_REQUEST_POLL_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_PULL_REQUEST_POLL_BATCH_SIZE}.")
        self.interval_seconds = _bounded_seconds(
            interval_seconds,
            MIN_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
            MAX_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
            "interval_seconds",
        )
        if type(sync_timeout_seconds) is not int or sync_timeout_seconds <= 0:
            raise ValueError("sync_timeout_seconds must be a positive integer.")
        if type(sync_retry_backoff_seconds) is not int or sync_retry_backoff_seconds <= 0:
            raise ValueError("sync_retry_backoff_seconds must be a positive integer.")
        self.pull_request_repository = pull_request_repository
        self.plan_repository = plan_repository
        self.custom_objects_api = custom_objects_api
        self.batch_size = batch_size
        self.sync_timeout_seconds = sync_timeout_seconds
        self.sync_retry_backoff_seconds = sync_retry_backoff_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ancestry_checker = ancestry_checker
        self.feedback = feedback
        self.feedback_interval_seconds = feedback_interval_seconds
        self._last_feedback_reconcile: datetime | None = None
        self._after_id: int | None = None
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> GitOpsSyncCycleSummary:
        """Observe one bounded rotating page and preserve retryable failures."""
        async with self._poll_lock:
            records = await self._next_page()
            selected = len(records)
            observed = synced = pending = failed = skipped = errors = 0
            seen: set[tuple[str, str, int, str]] = set()

            for record in records:
                reference = _sync_reference(record)
                if reference is None or reference in seen:
                    skipped += 1
                    record_sync_outcome("skipped")
                    continue
                seen.add(reference)
                observed += 1
                try:
                    result = await self._observe_record(record, reference)
                except Exception as error:
                    errors += 1
                    record_sync_outcome("error")
                    record_automation_failure("sync")
                    logger.warning("GitOps sync observation failed for a merged plan (%s).", type(error).__name__)
                    continue
                if result == "synced":
                    synced += 1
                    record_sync_outcome("synced")
                    record_sync_lag(_sync_lag_seconds(record, datetime.now(UTC)))
                elif result == "pending":
                    pending += 1
                    record_sync_outcome("pending")
                elif result == "failed":
                    failed += 1
                    record_sync_outcome("failed")
                    record_automation_failure("sync")
                elif result == "timeout":
                    failed += 1
                    record_sync_outcome("timeout")
                    record_sync_timeout()
                    record_automation_failure("sync")
                    record_sync_lag(_sync_lag_seconds(record, datetime.now(UTC)))
                else:
                    skipped += 1
                    record_sync_outcome("skipped")
                if result in {"synced", "failed", "timeout"} and self.feedback is not None:
                    # Post the sync step on the PR right away (J5); the publisher never raises.
                    await self.feedback.notify(reference[3])

            return GitOpsSyncCycleSummary(
                selected=selected,
                observed=observed,
                synced=synced,
                pending=pending,
                failed=failed,
                skipped=skipped,
                errors=errors,
            )

    async def run_once(self) -> GitOpsSyncCycleSummary:
        """One observation cycle and the PR feedback refresh; re-raises a failed poll afterwards.

        The controller loop registry (M8) calls this every sync interval.
        """
        failure: Exception | None = None
        summary = GitOpsSyncCycleSummary()
        try:
            summary = await self.poll_once()
        except Exception as error:
            logger.warning("GitOps sync poll cycle failed (%s).", type(error).__name__)
            failure = error
        else:
            if summary.selected:
                logger.info(
                    "GitOps sync poll completed: selected=%d synced=%d pending=%d failed=%d skipped=%d errors=%d",
                    summary.selected,
                    summary.synced,
                    summary.pending,
                    summary.failed,
                    summary.skipped,
                    summary.errors,
                )
        await self.reconcile_feedback()
        if failure is not None:
            raise failure
        return summary

    async def run(self, stop_event: asyncio.Event, *, should_run: Callable[[], bool] | None = None) -> None:
        """Poll immediately and repeat until stopped or cancelled."""
        while not stop_event.is_set():
            if should_run is None or should_run():
                try:
                    await self.run_once()
                except Exception:
                    pass
            else:
                logger.debug("Skipping GitOps sync observation while this controller is not the Lease holder.")

            interval = self.interval_seconds
            if should_run is not None and not should_run():
                interval = min(interval, DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def reconcile_feedback(self) -> None:
        """Refresh PR status comments (applied, measuring, outcome) every feedback interval (J5)."""
        if self.feedback is None:
            return
        now = _utc_now(self.clock())
        last = self._last_feedback_reconcile
        if last is not None and (now - last).total_seconds() < self.feedback_interval_seconds:
            return
        self._last_feedback_reconcile = now
        try:
            await self.feedback.reconcile_once()
        except Exception as error:
            logger.warning("PR status comment refresh failed (%s).", type(error).__name__)

    async def _observe_record(
        self,
        record: PullRequestRecord,
        reference: tuple[str, str, int, str],
    ) -> str:
        plan_id = reference[3]
        merge_sha = record.merge_commit_sha
        plan = await self.plan_repository.get_plan(plan_id)
        if plan is None or plan.plan_id != plan_id:
            return "skipped"
        if not _plan_is_syncable(plan.status):
            return "skipped"
        recommendation_ids = _valid_recommendation_ids(plan.recommendation_ids)
        if recommendation_ids is None:
            return "skipped"

        now = _utc_now(self.clock())
        if not isinstance(merge_sha, str) or GIT_OBJECT_SHA.fullmatch(merge_sha) is None:
            await self._mark_failed(record, plan, recommendation_ids, None, now, "invalid_merge_commit")
            return "failed"
        retry_after_timeout = plan.status is PlanStatus.SYNC_FAILED and record.sync_error_code == "sync_timeout"
        if retry_after_timeout and _sync_retry_backoff_active(
            record,
            now,
            self.sync_retry_backoff_seconds,
        ):
            return "pending"
        if plan.status is PlanStatus.SYNC_FAILED and not retry_after_timeout:
            await self._mark_failed(
                record,
                plan,
                recommendation_ids,
                merge_sha,
                now,
                record.sync_error_code or "sync_timeout",
            )
            return "failed"
        if plan.status is PlanStatus.APPLIED:
            await self._mark_synced(record, plan, recommendation_ids, merge_sha, now)
            return "synced"
        if not retry_after_timeout and _sync_timeout_elapsed(record, now, self.sync_timeout_seconds):
            await self._mark_failed(record, plan, recommendation_ids, merge_sha, now, "sync_timeout")
            return "timeout"

        ancestry = planned_changes = None
        if self.ancestry_checker is not None:
            ancestry = partial(self.ancestry_checker.is_ancestor, plan.source)
            # A descendant revision only confirms sync while it still carries
            # the planned edits; without the file plan that cannot be proven.
            file_plan = getattr(plan, "file_plan", None)
            if isinstance(file_plan, FilePlan):
                planned_changes = partial(self.ancestry_checker.planned_changes_present, plan.source, file_plan)
        result = await observe_gitops_sync(
            plan.source,
            merge_sha,
            self.custom_objects_api,
            ancestry=ancestry,
            planned_changes=planned_changes,
        )
        if result.status is GitOpsSyncStatus.SYNCED:
            await self._mark_synced(
                record,
                plan,
                recommendation_ids,
                result.observed_revision or merge_sha,
                now,
                reason=result.reason,
            )
            return "synced"
        if retry_after_timeout:
            if result.reason is GitOpsSyncReason.PLANNED_CHANGE_NOT_PRESENT:
                # A later commit reverted or overwrote the plan: that is final,
                # so stop re-observing the record like a plain ``sync_timeout``.
                await self._mark_failed(record, plan, recommendation_ids, merge_sha, now, result.reason.value)
                return "failed"
            await self._defer_timeout_retry(record, recommendation_ids, now)
            return "pending"
        if _sync_timeout_elapsed(record, now, self.sync_timeout_seconds):
            await self._mark_failed(record, plan, recommendation_ids, merge_sha, now, "sync_timeout")
            return "timeout"

        await self._mark_pending(record, plan, recommendation_ids, now, result.reason)
        return "pending"

    async def _defer_timeout_retry(
        self,
        record: PullRequestRecord,
        recommendation_ids: list[int],
        observed_at: datetime,
    ) -> None:
        linked_records = await self._linked_pull_request_records(record, recommendation_ids)
        await self._update_linked_pull_requests(
            linked_records,
            {
                "sync_status": "failed",
                "sync_observed_at": observed_at,
                "sync_error_code": "sync_timeout",
            },
        )

    async def _mark_synced(
        self,
        record: PullRequestRecord,
        plan: Plan,
        recommendation_ids: list[int],
        synced_revision: str,
        observed_at: datetime,
        *,
        reason: GitOpsSyncReason = GitOpsSyncReason.SYNC_CONFIRMED,
    ) -> None:
        action_updates: list[int] = []
        recovering_timeout = plan.status is PlanStatus.SYNC_FAILED and record.sync_error_code == "sync_timeout"
        for recommendation_id in recommendation_ids:
            status = await self.plan_repository.get_recommendation_action_status(recommendation_id)
            if status is ActionStatus.MERGED:
                action_updates.append(recommendation_id)
            elif status is ActionStatus.FAILED and recovering_timeout:
                action_updates.append(recommendation_id)
            elif status not in {ActionStatus.SYNC_PENDING, ActionStatus.APPLIED}:
                raise RuntimeError("plan action is not in a merge-compatible state")

        linked_records = await self._linked_pull_request_records(record, recommendation_ids)
        if plan.status in {PlanStatus.MERGED, PlanStatus.SYNC_FAILED}:
            updated_plan = await self.plan_repository.set_plan_status(
                plan.plan_id,
                PlanStatus.SYNCED,
                expected_statuses=(plan.status,),
                expected_digest=plan.digest,
                actor="gitops-sync-observer",
                reason_code=reason.value,
            )
            if updated_plan is None or updated_plan.status is not PlanStatus.SYNCED:
                raise RuntimeError("merged plan did not transition to synced")
        elif plan.status not in {PlanStatus.SYNCED, PlanStatus.APPLIED}:
            raise RuntimeError("plan is not in a merge-compatible state")

        if action_updates:
            updated_count = await self.plan_repository.set_recommendation_action_status(
                action_updates,
                ActionStatus.SYNC_PENDING,
                expected_statuses=(ActionStatus.MERGED, ActionStatus.FAILED),
                actor="gitops-sync-observer",
                plan_id=plan.plan_id,
            )
            if updated_count != len(action_updates):
                raise RuntimeError("not every recommendation action transitioned to sync-pending")

        await self._update_linked_pull_requests(
            linked_records,
            {
                "sync_status": "synced",
                "sync_revision": synced_revision,
                "sync_observed_at": observed_at,
                "sync_error_code": None,
            },
        )

    async def _mark_pending(
        self,
        record: PullRequestRecord,
        plan: Plan,
        recommendation_ids: list[int],
        observed_at: datetime,
        reason: GitOpsSyncReason,
    ) -> None:
        linked_records = await self._linked_pull_request_records(record, recommendation_ids)
        action_updates: list[int] = []
        for recommendation_id in recommendation_ids:
            status = await self.plan_repository.get_recommendation_action_status(recommendation_id)
            if status is ActionStatus.MERGED:
                action_updates.append(recommendation_id)
            elif status not in {ActionStatus.SYNC_PENDING, ActionStatus.APPLIED}:
                raise RuntimeError("plan action is not in a merge-compatible state")
        if plan.status not in {PlanStatus.MERGED, PlanStatus.SYNCED, PlanStatus.APPLIED}:
            raise RuntimeError("plan is not in a sync-compatible state")
        if action_updates:
            updated_count = await self.plan_repository.set_recommendation_action_status(
                action_updates,
                ActionStatus.SYNC_PENDING,
                expected_statuses=(ActionStatus.MERGED,),
                actor="gitops-sync-observer",
                plan_id=plan.plan_id,
            )
            if updated_count != len(action_updates):
                raise RuntimeError("not every recommendation action transitioned to sync-pending")
        await self._update_linked_pull_requests(
            linked_records,
            {
                "sync_status": "pending",
                "sync_revision": None,
                "sync_observed_at": observed_at,
                "sync_error_code": reason.value,
            },
        )

    async def _mark_failed(
        self,
        record: PullRequestRecord,
        plan: Plan,
        recommendation_ids: list[int],
        merge_sha: str | None,
        observed_at: datetime,
        error_code: str,
    ) -> None:
        action_updates: list[int] = []
        for recommendation_id in recommendation_ids:
            status = await self.plan_repository.get_recommendation_action_status(recommendation_id)
            if status in {ActionStatus.MERGED, ActionStatus.SYNC_PENDING}:
                action_updates.append(recommendation_id)
            elif status not in {ActionStatus.APPLIED, ActionStatus.FAILED}:
                raise RuntimeError("plan action is not in a sync-failure-compatible state")

        linked_records = await self._linked_pull_request_records(record, recommendation_ids)
        first_failure = plan.status is not PlanStatus.SYNC_FAILED
        if plan.status in {PlanStatus.MERGED, PlanStatus.SYNCED}:
            updated_plan = await self.plan_repository.set_plan_status(
                plan.plan_id,
                PlanStatus.SYNC_FAILED,
                expected_statuses=(plan.status,),
                expected_digest=plan.digest,
                actor="gitops-sync-observer",
                reason_code=error_code,
            )
            if updated_plan is None or updated_plan.status is not PlanStatus.SYNC_FAILED:
                raise RuntimeError("plan did not transition to sync-failed")

        if action_updates:
            updated_count = await self.plan_repository.set_recommendation_action_status(
                action_updates,
                ActionStatus.FAILED,
                expected_statuses=(ActionStatus.MERGED, ActionStatus.SYNC_PENDING),
                actor="gitops-sync-observer",
                plan_id=plan.plan_id,
            )
            if updated_count != len(action_updates):
                raise RuntimeError("not every recommendation action transitioned to failed")

        await self._update_linked_pull_requests(
            linked_records,
            {
                "sync_status": "failed",
                "sync_revision": merge_sha,
                "sync_observed_at": observed_at,
                "sync_error_code": error_code,
            },
        )
        if first_failure:
            logger.error("GitOps plan %s sync did not complete (%s).", plan.plan_id, error_code)

    async def _linked_pull_request_records(
        self,
        record: PullRequestRecord,
        recommendation_ids: list[int] | None,
    ) -> list[PullRequestRecord]:
        if (
            not isinstance(record.provider_id, str)
            or not isinstance(record.repo, str)
            or type(record.pr_number) is not int
        ):
            raise RuntimeError("merged pull request is missing its provider reference")
        linked_records = await self.pull_request_repository.list_pull_requests_by_provider_reference(
            record.provider_id,
            record.repo,
            record.pr_number,
        )
        if not linked_records:
            raise RuntimeError("no pull-request records match the provider reference")
        if any(linked.plan_id != record.plan_id for linked in linked_records):
            raise RuntimeError("grouped pull-request records reference different plans")
        if recommendation_ids is not None and {linked.recommendation_id for linked in linked_records} != set(
            recommendation_ids
        ):
            raise RuntimeError("grouped pull-request records do not match the persisted plan")
        if any(type(linked.id) is not int or linked.id <= 0 for linked in linked_records):
            raise RuntimeError("linked pull-request record is missing its database ID")
        return linked_records

    async def _update_linked_pull_requests(
        self,
        linked_records: list[PullRequestRecord],
        updates: dict[str, Any],
    ) -> None:
        for linked in linked_records:
            if linked.id is None:
                raise RuntimeError("linked pull-request record is missing its database ID")
            status = getattr(linked.status, "value", linked.status)
            if not isinstance(status, str) or not isinstance(linked.plan_id, str):
                raise RuntimeError("linked pull-request record is missing its current state")
            await self.pull_request_repository.update_pull_request(
                linked.id,
                updates.copy(),
                expected_statuses=(status,),
                expected_plan_id=linked.plan_id,
            )

    async def _next_page(self) -> list[PullRequestRecord]:
        records = await self.pull_request_repository.list_sync_pending_pull_requests(
            limit=self.batch_size,
            after_id=self._after_id,
        )
        if not records and self._after_id is not None:
            self._after_id = None
            records = await self.pull_request_repository.list_sync_pending_pull_requests(limit=self.batch_size)
        ids = [record.id for record in records if type(record.id) is int and record.id > 0]
        self._after_id = max(ids) if len(records) == self.batch_size and ids else None
        return records


def create_gitops_sync_runtime(
    pull_request_repository: SyncPullRequestRepository,
    plan_repository: SyncPlanRepository,
    *,
    custom_objects_api: Any | None = None,
    batch_size: int = MAX_PULL_REQUEST_POLL_BATCH_SIZE,
    interval_seconds: int | float | None = None,
    sync_timeout_seconds: int | None = None,
    sync_retry_backoff_seconds: int = DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS,
    ancestry_checker: GitAncestryChecker | None = None,
    provider_resolver: PlanProviderResolver | None = None,
    feedback: PullRequestFeedback | None = None,
) -> GitOpsSyncRuntime:
    """Compose periodic sync observation using a lazy, shared Kubernetes API client.

    Ancestry checks use ``ancestry_checker``, else ``provider_resolver``, else
    an authorized provider registry built from ``plan_repository`` when it
    stores provider configurations (the production GitOps repository).
    """
    if ancestry_checker is None:
        if provider_resolver is None:
            provider_resolver = _default_provider_resolver(plan_repository)
        if provider_resolver is not None:
            ancestry_checker = ProviderAncestryChecker(provider_resolver)
    return GitOpsSyncRuntime(
        pull_request_repository,
        plan_repository,
        custom_objects_api if custom_objects_api is not None else _LazyCustomObjectsApi(),
        batch_size=batch_size,
        interval_seconds=(parse_gitops_sync_poll_interval_seconds() if interval_seconds is None else interval_seconds),
        sync_timeout_seconds=(
            parse_gitops_sync_timeout_seconds() if sync_timeout_seconds is None else sync_timeout_seconds
        ),
        sync_retry_backoff_seconds=sync_retry_backoff_seconds,
        ancestry_checker=ancestry_checker,
        feedback=feedback,
    )


def _default_provider_resolver(plan_repository: object) -> PlanProviderResolver | None:
    """Mirror the plan runtime's authorized provider resolution (mounted Secret tokens)."""
    from greenkube.automation.git.k8s_secret_resolver import MountedSecretFileResolver
    from greenkube.automation.git.registry import GitProviderRegistry
    from greenkube.automation.plan_worker import RegistryPlanProviderResolver
    from greenkube.storage.base_gitops_repository import GitOpsProviderRepository

    if not isinstance(plan_repository, GitOpsProviderRepository):
        return None
    registry = GitProviderRegistry(plan_repository, MountedSecretFileResolver())
    return RegistryPlanProviderResolver(plan_repository, registry)


class _LazyCustomObjectsApi:
    def __init__(self) -> None:
        self._api: Any | None = None

    async def list_namespaced_custom_object(self, **kwargs: Any) -> Any:
        if self._api is None:
            from greenkube.core.k8s_client import get_custom_objects_api

            api = await get_custom_objects_api()
            if api is None:
                raise RuntimeError("Kubernetes custom objects API is unavailable.")
            self._api = api
        return await self._api.list_namespaced_custom_object(**kwargs)


def parse_gitops_sync_poll_interval_seconds(value: object | None = None) -> int:
    """Parse the bounded periodic sync-observation interval."""
    return _parse_seconds(
        value,
        environment_name=GITOPS_SYNC_POLL_INTERVAL_ENV,
        default=DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
        minimum=MIN_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
        maximum=MAX_GITOPS_SYNC_POLL_INTERVAL_SECONDS,
    )


def parse_gitops_sync_timeout_seconds(value: object | None = None) -> int:
    """Parse the bounded timeout before a merged but unsynced plan fails."""
    return _parse_seconds(
        value,
        environment_name=GITOPS_SYNC_TIMEOUT_ENV,
        default=DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS,
        minimum=MIN_GITOPS_SYNC_TIMEOUT_SECONDS,
        maximum=MAX_GITOPS_SYNC_TIMEOUT_SECONDS,
    )


def _sync_reference(record: PullRequestRecord) -> tuple[str, str, int, str] | None:
    if (
        type(record.id) is not int
        or record.id <= 0
        or not isinstance(record.provider_id, str)
        or not record.provider_id
        or record.provider_id != record.provider_id.strip()
        or not isinstance(record.repo, str)
        or not isinstance(record.plan_id, str)
        or not record.plan_id
        or record.plan_id != record.plan_id.strip()
        or type(record.pr_number) is not int
        or record.pr_number <= 0
    ):
        return None
    return record.provider_id, record.repo, record.pr_number, record.plan_id


def _valid_recommendation_ids(values: object) -> list[int] | None:
    if (
        not isinstance(values, list)
        or not values
        or any(type(value) is not int or value <= 0 for value in values)
        or len(values) != len(set(values))
    ):
        return None
    return values


def _plan_is_syncable(status: PlanStatus) -> bool:
    return status in {PlanStatus.MERGED, PlanStatus.SYNCED, PlanStatus.APPLIED, PlanStatus.SYNC_FAILED}


def _sync_timeout_elapsed(record: PullRequestRecord, now: datetime, timeout_seconds: int) -> bool:
    for started_at in (record.merged_at, record.created_at):
        if isinstance(started_at, datetime) and started_at.tzinfo is not None and started_at.utcoffset() is not None:
            return (now - started_at.astimezone(UTC)).total_seconds() >= timeout_seconds
    return False


def _sync_retry_backoff_active(record: PullRequestRecord, now: datetime, backoff_seconds: int) -> bool:
    last_attempt = record.sync_observed_at
    if not isinstance(last_attempt, datetime) or last_attempt.tzinfo is None or last_attempt.utcoffset() is None:
        return False
    return (now - last_attempt.astimezone(UTC)).total_seconds() < backoff_seconds


def _sync_lag_seconds(record: PullRequestRecord, now: datetime) -> float:
    for started_at in (record.merged_at, record.created_at):
        if isinstance(started_at, datetime) and started_at.tzinfo is not None and started_at.utcoffset() is not None:
            return max(0.0, (now - started_at.astimezone(UTC)).total_seconds())
    return 0.0


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sync runtime clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _bounded_seconds(value: object, minimum: int, maximum: int, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite positive number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} seconds")
    return float(value)


def _parse_seconds(
    value: object | None,
    *,
    environment_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.environ.get(environment_name) if value is None else value
    if raw_value is None:
        return default
    if (
        not isinstance(raw_value, str)
        or not raw_value
        or raw_value != raw_value.strip()
        or not raw_value.isascii()
        or not raw_value.isdecimal()
    ):
        raise ValueError(f"{environment_name} must be an integer number of seconds.")
    seconds = int(raw_value)
    if not minimum <= seconds <= maximum:
        raise ValueError(f"{environment_name} must be between {minimum} and {maximum} seconds.")
    return seconds
