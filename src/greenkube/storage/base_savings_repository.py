# src/greenkube/storage/base_savings_repository.py
"""Abstract interface for the recommendation savings ledger repository."""

from abc import ABC, abstractmethod
from typing import Dict, List

from ..models.savings import SavingsLedgerRecord


class SavingsLedgerRepository(ABC):
    """Port for persisting and querying the recommendation savings ledger."""

    @abstractmethod
    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        """Persist a batch of raw period savings records.

        Args:
            records: Records computed by SavingsAttributor for the current period.

        Returns:
            Number of rows inserted.
        """

    @abstractmethod
    async def get_cumulative_totals(self, cluster_name: str) -> Dict[str, Dict[str, float]]:
        """Return cumulative savings grouped by recommendation_type.

        Queries both the raw ledger and the hourly aggregates, combining
        their totals so the caller always sees the full picture regardless
        of compression state.

        Returns:
            ``{recommendation_type: {"co2e_saved_grams": float, "cost_saved_dollars": float}}``
        """

    @abstractmethod
    async def compress_to_hourly(self, cutoff_hours: int = 24) -> int:
        """Aggregate raw records older than *cutoff_hours* into hourly buckets.

        Returns:
            Number of hourly rows upserted.
        """

    @abstractmethod
    async def prune_raw(self, retention_days: int = 7) -> int:
        """Delete raw records older than *retention_days*.

        Returns:
            Number of rows deleted.
        """
