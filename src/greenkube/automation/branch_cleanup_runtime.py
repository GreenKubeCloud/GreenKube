"""Crash-recoverable cleanup for GreenKube's deterministic plan branches."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

import structlog

from greenkube.automation.git.base import (
    GitProviderCapability,
    GitProviderCapabilityError,
    GitProviderError,
    GitProviderTransientError,
    GitRepository,
    PullRequestInfo,
    validate_git_ref,
    validate_greenkube_plan_branch,
)
from greenkube.automation.plan_worker import GitProviderTarget, plan_branch_name
from greenkube.gitops.contracts import OperationStatus, Plan, PlanStatus
from greenkube.models.metrics import PullRequestRecord
from greenkube.storage.base_pull_request_repository import (
    MAX_BRANCH_CLEANUP_BATCH_SIZE,
)

logger = structlog.get_logger(__name__)
_RESULT = TypeVar("_RESULT")

_TERMINAL_PLAN_STATUSES = frozenset(
    {
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
_ACTIVE_OPERATION_STATUSES = frozenset(
    {OperationStatus.QUEUED.value, OperationStatus.RUNNING.value, OperationStatus.RETRYING.value}
)
_TERMINAL_PULL_REQUEST_STATUSES = frozenset({"closed", "merged"})
_OPEN_PULL_REQUEST_STATUSES = frozenset({"open", "opened"})
_PROVIDER_TERMINAL_PULL_REQUEST_STATUSES = frozenset({"closed", "merged"})
DEFAULT_BRANCH_CLEANUP_PROVIDER_TIMEOUT_SECONDS = 30.0


class _BranchCleanupFailure(Exception):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause


def safe_gitops_error_fields(error: Exception, *, stage: str) -> dict[str, object]:
    """Return structured failure metadata without provider messages or credentials."""
    error_type = type(error).__name__
    status_code = getattr(error, "status_code", None)
    status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
    return {
        "failure_stage": stage,
        "failure_code": _safe_gitops_error_code(error),
        "failure_type": error_type,
        "provider_status_code": status_code,
    }


def _safe_gitops_error_code(error: Exception) -> str:
    if isinstance(error, GitProviderCapabilityError):
        return "provider_capability_missing"
    if isinstance(error, GitProviderTransientError):
        return "provider_transient"
    if isinstance(error, GitProviderError):
        return "provider_http_error" if getattr(error, "status_code", None) is not None else "provider_error"
    if isinstance(error, TimeoutError):
        return "operation_timeout"
    if type(error).__name__ == "PullRequestPollingLeaseNotOwnedError":
        return "polling_lease_lost"

    error_type = type(error)
    if error_type.__module__.startswith("asyncpg"):
        message = str(error).casefold()
        if "no space left on device" in message or "could not extend file" in message:
            return "storage_capacity_exhausted"
        return "storage_database_error"
    if error_type.__name__ == "OperationalError" and error_type.__module__.startswith(("sqlite3", "asyncpg")):
        message = str(error).casefold()
        if "database or disk is full" in message or "no space left on device" in message:
            return "storage_capacity_exhausted"
        if "database is locked" in message or "database is busy" in message:
            return "storage_busy"
        return "storage_operational_error"
    return "unexpected_error"


def is_retryable_gitops_error(error: Exception) -> bool:
    return _safe_gitops_error_code(error) in {
        "operation_timeout",
        "polling_lease_lost",
        "provider_transient",
        "storage_busy",
        "storage_capacity_exhausted",
        "storage_database_error",
        "storage_operational_error",
    }


async def _at_stage(stage: str, operation: Awaitable[_RESULT]) -> _RESULT:
    try:
        return await operation
    except asyncio.CancelledError:
        raise
    except _BranchCleanupFailure:
        raise
    except Exception as exc:
        raise _BranchCleanupFailure(stage, exc) from None


class _CleanupCandidateRepository(Protocol):
    async def list_branch_cleanup_candidates(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> list[PullRequestRecord]: ...

    async def list_pull_requests_by_plan_id(self, plan_id: str) -> list[PullRequestRecord]: ...

    async def mark_plan_branch_cleanup_complete(
        self,
        plan_id: str,
        *,
        provider_id: str,
        repo: str,
        branch_name: str,
    ) -> None: ...


class _CleanupPlanRepository(Protocol):
    async def get_plan(self, plan_id: str) -> Plan | None: ...


class _CleanupOperationRepository(Protocol):
    async def get_by_plan_id(self, plan_id: str) -> object | None: ...


class _PlanProviderResolver(Protocol):
    async def resolve(self, source) -> GitProviderTarget: ...


class _CleanupGitProvider(Protocol):
    name: str

    def supports(self, capability: GitProviderCapability | str) -> bool: ...

    async def get_default_branch(self, repo: GitRepository) -> str: ...

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo: ...

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> PullRequestInfo | None: ...

    async def delete_plan_branch(self, repo: GitRepository, branch: str) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BranchCleanupSummary:
    """Secret-free counts from one bounded cleanup pass."""

    selected: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0


class BranchCleanupRuntime:
    """Revalidate persisted plan ownership and provider state before deleting a branch.

    Candidate rows remain pending until the provider confirms deletion or
    absence. A later pass therefore safely recovers crashes between remote
    deletion and local completion persistence.
    """

    def __init__(
        self,
        *,
        pull_request_repository: _CleanupCandidateRepository,
        plan_repository: _CleanupPlanRepository,
        operation_repository: _CleanupOperationRepository,
        provider_resolver: _PlanProviderResolver,
        batch_size: int = MAX_BRANCH_CLEANUP_BATCH_SIZE,
        provider_timeout_seconds: float = DEFAULT_BRANCH_CLEANUP_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_BRANCH_CLEANUP_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BRANCH_CLEANUP_BATCH_SIZE}.")
        if (
            isinstance(provider_timeout_seconds, bool)
            or not isinstance(provider_timeout_seconds, (int, float))
            or not math.isfinite(provider_timeout_seconds)
            or provider_timeout_seconds <= 0
        ):
            raise ValueError("provider_timeout_seconds must be positive.")
        self.pull_request_repository = pull_request_repository
        self.plan_repository = plan_repository
        self.operation_repository = operation_repository
        self.provider_resolver = provider_resolver
        self.batch_size = batch_size
        self.provider_timeout_seconds = float(provider_timeout_seconds)
        self._after_id: int | None = None
        self._cleanup_lock = asyncio.Lock()

    async def cleanup_once(
        self,
        *,
        heartbeat: Callable[[], Awaitable[object]] | None = None,
    ) -> BranchCleanupSummary:
        """Check one bounded page of terminal or provider-closed plan branches."""
        async with self._cleanup_lock:
            try:
                candidates = await self._next_page()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "GitOps plan branch cleanup candidate query failed; it will be retried.",
                    **safe_gitops_error_fields(exc, stage="candidate_query"),
                )
                return BranchCleanupSummary(errors=1)
            groups: dict[str, list[PullRequestRecord]] = {}
            for record in candidates:
                if isinstance(record.plan_id, str) and record.plan_id:
                    groups.setdefault(record.plan_id, []).append(record)

            deleted = skipped = errors = 0
            for plan_id, page_records in groups.items():
                try:
                    if await self._cleanup_plan(plan_id, page_records, heartbeat):
                        deleted += 1
                    else:
                        skipped += 1
                except _BranchCleanupFailure as failure:
                    errors += 1
                    logger.warning(
                        "GitOps plan branch cleanup failed; the candidate will be retried.",
                        **safe_gitops_error_fields(failure.cause, stage=failure.stage),
                    )
                except Exception as exc:
                    errors += 1
                    logger.warning(
                        "GitOps plan branch cleanup failed; the candidate will be retried.",
                        **safe_gitops_error_fields(exc, stage="candidate_processing"),
                    )
            skipped += len(candidates) - sum(len(records) for records in groups.values())
            summary = BranchCleanupSummary(
                selected=len(groups),
                deleted=deleted,
                skipped=skipped,
                errors=errors,
            )
            if summary.selected or summary.errors:
                logger.info(
                    "GitOps plan branch cleanup pass completed.",
                    branch_cleanup_selected=summary.selected,
                    branch_cleanup_deleted=summary.deleted,
                    branch_cleanup_skipped=summary.skipped,
                    branch_cleanup_errors=summary.errors,
                )
            return summary

    async def _next_page(self) -> list[PullRequestRecord]:
        records = await self.pull_request_repository.list_branch_cleanup_candidates(
            limit=self.batch_size,
            after_id=self._after_id,
        )
        if not records and self._after_id is not None:
            self._after_id = None
            records = await self.pull_request_repository.list_branch_cleanup_candidates(
                limit=self.batch_size,
                after_id=None,
            )
        record_ids = [record.id for record in records if type(record.id) is int and record.id > 0]
        self._after_id = max(record_ids) if len(records) == self.batch_size and record_ids else None
        return records

    async def _cleanup_plan(
        self,
        plan_id: str,
        page_records: list[PullRequestRecord],
        heartbeat: Callable[[], Awaitable[object]] | None,
    ) -> bool:
        plan = await _at_stage("plan_lookup", self.plan_repository.get_plan(plan_id))
        if plan is None or plan.plan_id != plan_id:
            return False
        try:
            expected_branch = plan_branch_name(plan)
            validate_greenkube_plan_branch(expected_branch)
        except (GitProviderError, TypeError, ValueError):
            return False

        records = await _at_stage(
            "plan_record_lookup",
            self.pull_request_repository.list_pull_requests_by_plan_id(plan_id),
        )
        if not _records_match_plan(records, plan, expected_branch):
            return False
        if not any(_same_record(record, page_record) for record in records for page_record in page_records):
            return False

        operation = await _at_stage(
            "operation_lookup",
            self.operation_repository.get_by_plan_id(plan_id),
        )
        if operation is not None and _status_value(getattr(operation, "status", None)) in _ACTIVE_OPERATION_STATUSES:
            return False
        if operation is not None and _status_value(getattr(operation, "status", None)) not in {
            *_ACTIVE_OPERATION_STATUSES,
            OperationStatus.SUCCEEDED.value,
            OperationStatus.FAILED.value,
            OperationStatus.CANCELLED.value,
        }:
            return False

        terminal_plan = plan.status in _TERMINAL_PLAN_STATUSES
        terminal_linked_pr = any(_status_value(record.status) in _TERMINAL_PULL_REQUEST_STATUSES for record in records)
        if not terminal_plan and not terminal_linked_pr:
            return False

        target = await _at_stage("provider_resolution", self.provider_resolver.resolve(plan.source))
        provider = cast(_CleanupGitProvider, target.provider)
        try:
            if not _target_matches_records(target, records, plan):
                return False
            if not provider.supports(GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH):
                return False
            if not provider.supports(GitProviderCapability.DELETE_PLAN_BRANCH):
                return False

            branch = expected_branch
            base_branch = plan.source.ref
            if branch == base_branch:
                return False
            default_branch = await self._provider_call(
                provider.get_default_branch,
                target.repository,
                stage="default_branch_lookup",
                heartbeat=heartbeat,
            )
            try:
                default_branch = validate_git_ref(default_branch, field="provider default branch")
            except (GitProviderError, TypeError):
                return False
            if branch == default_branch:
                return False

            pull_request_numbers = {record.pr_number for record in records}
            if len(pull_request_numbers) != 1:
                return False
            pull_request_number = next(iter(pull_request_numbers))
            if pull_request_number is not None:
                if type(pull_request_number) is not int or pull_request_number <= 0:
                    return False
                current = await self._provider_call(
                    provider.get_pull_request,
                    target.repository,
                    pull_request_number,
                    stage="linked_pull_request_lookup",
                    heartbeat=heartbeat,
                )
                if not _provider_pr_is_terminal_for_branch(current, pull_request_number, branch, base_branch):
                    return False

            open_pull_request = await self._provider_call(
                provider.find_open_pull_request_for_branch,
                target.repository,
                branch,
                stage="open_pull_request_lookup",
                heartbeat=heartbeat,
            )
            if open_pull_request is not None:
                return False

            await self._provider_call(
                provider.delete_plan_branch,
                target.repository,
                branch,
                stage="branch_delete",
                heartbeat=heartbeat,
            )
            await _at_stage(
                "completion_persistence",
                self.pull_request_repository.mark_plan_branch_cleanup_complete(
                    plan_id,
                    provider_id=plan.source.provider_id,
                    repo=target.repository.full_name,
                    branch_name=branch,
                ),
            )
            return True
        finally:
            try:
                await provider.close()
            except Exception:
                logger.debug("Git provider client close failed after plan branch cleanup.")

    async def _provider_call(
        self,
        method: Callable[..., Awaitable[_RESULT]],
        *args,
        stage: str,
        heartbeat: Callable[[], Awaitable[object]] | None = None,
    ) -> _RESULT:
        if heartbeat is not None:
            await _at_stage(stage, heartbeat())
        return await _at_stage(
            stage,
            asyncio.wait_for(method(*args), timeout=self.provider_timeout_seconds),
        )


def _records_match_plan(records: list[PullRequestRecord], plan: Plan, branch: str) -> bool:
    recommendation_ids = tuple(plan.recommendation_ids)
    if not recommendation_ids or len(records) != len(recommendation_ids):
        return False
    if {record.recommendation_id for record in records} != set(recommendation_ids):
        return False
    if any(type(record.id) is not int or record.id <= 0 for record in records):
        return False
    references = {
        (
            record.provider,
            record.provider_id,
            record.repo,
            record.base_branch,
            record.head_branch,
            record.pr_number,
        )
        for record in records
    }
    if len(references) != 1:
        return False
    provider, provider_id, _, base_branch, head_branch, pr_number = next(iter(references))
    if (
        not isinstance(provider, str)
        or not isinstance(provider_id, str)
        or provider_id != plan.source.provider_id
        or base_branch != plan.source.ref
        or head_branch != branch
        or (pr_number is not None and (type(pr_number) is not int or pr_number <= 0))
    ):
        return False
    return True


def _target_matches_records(target: GitProviderTarget, records: list[PullRequestRecord], plan: Plan) -> bool:
    if not records:
        return False
    provider = target.provider
    repository = target.repository
    expected_provider = records[0].provider
    return (
        isinstance(expected_provider, str)
        and provider.name == expected_provider
        and repository.provider == provider.name
        and repository.full_name == records[0].repo
        and records[0].provider_id == plan.source.provider_id
    )


def _provider_pr_is_terminal_for_branch(
    pull_request: PullRequestInfo,
    expected_number: int,
    expected_head: str,
    expected_base: str,
) -> bool:
    if (
        not isinstance(pull_request, PullRequestInfo)
        or type(pull_request.number) is not int
        or pull_request.number != expected_number
        or type(pull_request.merged) is not bool
        or not isinstance(pull_request.status, str)
        or pull_request.status != pull_request.status.strip()
        or pull_request.head_branch != expected_head
        or pull_request.base_branch != expected_base
    ):
        return False
    status = pull_request.status.casefold()
    if status in _OPEN_PULL_REQUEST_STATUSES:
        return False
    if status == "merged":
        return pull_request.merged
    return status in _PROVIDER_TERMINAL_PULL_REQUEST_STATUSES


def _same_record(left: PullRequestRecord, right: PullRequestRecord) -> bool:
    return left.id is not None and left.id == right.id


def _status_value(value: object) -> str | None:
    selected = getattr(value, "value", value)
    return selected if isinstance(selected, str) else None
