# src/greenkube/storage/base_pull_request_repository.py
"""Abstract interface for recommendation pull-request tracking."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from typing import List, Optional

from ..models.metrics import PullRequestRecord

MAX_PULL_REQUEST_POLL_BATCH_SIZE = 100
MAX_BRANCH_CLEANUP_BATCH_SIZE = 100
ORPHANED_PENDING_PULL_REQUEST_ERROR = "orphaned_pending_attempt_missing_plan_provider_reference"
BRANCH_CLEANUP_PLAN_STATUSES = (
    "merged",
    "synced",
    "applied",
    "sync_failed",
    "closed",
    "stale",
    "failed",
    "needs_mapping",
    "ambiguous",
    "rejected",
    "superseded",
)


class PullRequestStateConflictError(RuntimeError):
    """Raised when a pull-request update no longer matches its expected state."""


class PullRequestRepository(ABC):
    """Port for persisting pull requests opened by the automation bot."""

    @abstractmethod
    async def save_pull_request(self, record: PullRequestRecord) -> PullRequestRecord:
        """Persists a new pull-request attempt.

        Args:
            record: The attempt to store.

        Returns:
            The stored record with its database ID.
        """

    @abstractmethod
    async def update_pull_request(
        self,
        pr_id: int,
        updates: dict,
        *,
        expected_statuses: Sequence[str] | None = None,
        expected_plan_id: str | None = None,
    ) -> PullRequestRecord:
        """Updates mutable columns of a pull-request attempt.

        Args:
            pr_id: The database primary key.
            updates: ``{column: value}`` pairs restricted to known columns.

        Returns:
            The updated record.
        """

    @abstractmethod
    async def get_pull_request_by_id(self, pr_id: int) -> Optional[PullRequestRecord]:
        """Returns a single attempt by its database ID."""

    @abstractmethod
    async def get_pull_request_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> Optional[PullRequestRecord]:
        """Returns a pull request by its provider, repository and provider-side number."""

    @abstractmethod
    async def list_pull_requests_by_provider_reference(
        self, provider_id: str, repo: str, pr_number: int
    ) -> List[PullRequestRecord]:
        """Returns every recommendation record linked to one provider pull request."""

    @abstractmethod
    async def get_pull_request_by_plan_id(self, plan_id: str) -> Optional[PullRequestRecord]:
        """Returns the most recent pull request attempt associated with a plan."""

    async def list_pull_requests_by_plan_id(self, plan_id: str) -> List[PullRequestRecord]:
        """Returns all recommendation records associated with a plan."""
        record = await self.get_pull_request_by_plan_id(plan_id)
        return [record] if record is not None else []

    @abstractmethod
    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> List[PullRequestRecord]:
        """Returns every attempt for a recommendation, newest first."""

    @abstractmethod
    async def get_open_pull_requests(self, recommendation_id: Optional[int] = None) -> List[PullRequestRecord]:
        """Returns attempts still considered open/pending, optionally for one recommendation."""

    @abstractmethod
    async def list_pollable_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        """Returns a bounded page of plan-linked open attempts in ascending ID order."""

    @abstractmethod
    async def list_sync_pending_pull_requests(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        """Returns merged PRs awaiting sync observation, including timed-out attempts for recovery."""

    @abstractmethod
    async def reconcile_open_pull_requests(self, *, stale_before: datetime, limit: int) -> int:
        """Quarantine bounded stale pending rows lacking plan, provider, and PR references.

        Plan-linked attempts remain pending for provider-aware polling and worker
        recovery. Rows without immutable provider linkage cannot be reconciled
        safely and are marked as orphaned rather than treated as open PRs.
        """

    async def list_branch_cleanup_candidates(
        self,
        *,
        limit: int,
        after_id: int | None = None,
    ) -> List[PullRequestRecord]:
        """Returns bounded, uncompleted plan branches eligible for safety revalidation."""
        self.validate_cleanup_page(limit, after_id)
        raise NotImplementedError("branch cleanup candidate listing is not supported by this repository")

    async def mark_plan_branch_cleanup_complete(
        self,
        plan_id: str,
        *,
        provider_id: str,
        repo: str,
        branch_name: str,
    ) -> None:
        """Persist successful deletion or absence for one deterministic plan branch."""
        raise NotImplementedError("branch cleanup completion persistence is not supported by this repository")

    @staticmethod
    def validate_poll_page(limit: int, after_id: int | None) -> None:
        """Reject invalid or unbounded poll-page requests consistently across adapters."""
        if type(limit) is not int or not 1 <= limit <= MAX_PULL_REQUEST_POLL_BATCH_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PULL_REQUEST_POLL_BATCH_SIZE}.")
        if after_id is not None and (type(after_id) is not int or after_id < 0):
            raise ValueError("after_id must be a non-negative integer.")

    @staticmethod
    def validate_cleanup_page(limit: int, after_id: int | None) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_BRANCH_CLEANUP_BATCH_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_BRANCH_CLEANUP_BATCH_SIZE}.")
        if after_id is not None and (type(after_id) is not int or after_id < 0):
            raise ValueError("after_id must be a non-negative integer.")
