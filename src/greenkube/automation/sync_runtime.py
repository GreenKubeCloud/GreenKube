"""Bounded GitOps sync observation and plan lifecycle transitions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from greenkube.core.leader_election import DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS
from greenkube.core.observability import (
    record_automation_failure,
    record_sync_lag,
    record_sync_outcome,
    record_sync_timeout,
)
from greenkube.gitops.contracts import ActionStatus, Plan, PlanStatus
from greenkube.gitops.sync import GitOpsSyncReason, GitOpsSyncStatus, observe_gitops_sync
from greenkube.models.metrics import PullRequestRecord
from greenkube.storage.base_pull_request_repository import MAX_PULL_REQUEST_POLL_BATCH_SIZE

DEFAULT_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 30
MIN_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 10
MAX_GITOPS_SYNC_POLL_INTERVAL_SECONDS = 86_400
DEFAULT_GITOPS_SYNC_TIMEOUT_SECONDS = 1800
MIN_GITOPS_SYNC_TIMEOUT_SECONDS = 60
MAX_GITOPS_SYNC_TIMEOUT_SECONDS = 86_400
DEFAULT_GITOPS_SYNC_RETRY_BACKOFF_SECONDS = 30
GITOPS_SYNC_POLL_INTERVAL_ENV = "GITOPS_SYNC_POLL_INTERVAL"
GITOPS_SYNC_TIMEOUT_ENV = "GITOPS_SYNC_TIMEOUT"
_COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

logger = logging.getLogger(__name__)


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

            return GitOpsSyncCycleSummary(
                selected=selected,
                observed=observed,
                synced=synced,
                pending=pending,
                failed=failed,
                skipped=skipped,
                errors=errors,
            )

    async def run(self, stop_event: asyncio.Event, *, should_run: Callable[[], bool] | None = None) -> None:
        """Poll immediately and repeat until stopped or cancelled."""
        while not stop_event.is_set():
            if should_run is None or should_run():
                try:
                    summary = await self.poll_once()
                except Exception as error:
                    logger.warning("GitOps sync poll cycle failed (%s).", type(error).__name__)
                else:
                    if summary.selected:
                        logger.info(
                            "GitOps sync poll completed: "
                            "selected=%d synced=%d pending=%d failed=%d skipped=%d errors=%d",
                            summary.selected,
                            summary.synced,
                            summary.pending,
                            summary.failed,
                            summary.skipped,
                            summary.errors,
                        )
            else:
                logger.debug("Skipping GitOps sync observation while this controller is not the Lease holder.")

            interval = self.interval_seconds
            if should_run is not None and not should_run():
                interval = min(interval, DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

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
        if not isinstance(merge_sha, str) or _COMMIT_SHA.fullmatch(merge_sha) is None:
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

        result = await observe_gitops_sync(plan.source, merge_sha, self.custom_objects_api)
        if result.status is GitOpsSyncStatus.SYNCED:
            await self._mark_synced(record, plan, recommendation_ids, merge_sha, now)
            return "synced"
        if retry_after_timeout:
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
        merge_sha: str,
        observed_at: datetime,
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
                "sync_revision": merge_sha,
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
) -> GitOpsSyncRuntime:
    """Compose periodic sync observation using a lazy, shared Kubernetes API client."""
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
    )


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
