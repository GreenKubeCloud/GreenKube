# src/greenkube/storage/base_pull_request_repository.py
"""Abstract interface for recommendation pull-request tracking."""

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models.metrics import PullRequestRecord


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
    async def update_pull_request(self, pr_id: int, updates: dict) -> PullRequestRecord:
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
    async def get_pull_requests_for_recommendation(self, recommendation_id: int) -> List[PullRequestRecord]:
        """Returns every attempt for a recommendation, newest first."""

    @abstractmethod
    async def get_open_pull_requests(self, recommendation_id: Optional[int] = None) -> List[PullRequestRecord]:
        """Returns attempts still considered open/pending, optionally for one recommendation."""
