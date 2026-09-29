# src/greenkube/storage/base_savings_repository.py
"""Abstract interface for the recommendation savings ledger repository."""

from abc import ABC, abstractmethod
from datetime import datetime
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
            Number of rows inserted or updated.
        """

    @abstractmethod
    async def get_cumulative_totals(
        self,
        cluster_name: str,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Return cumulative savings grouped by recommendation_type.

        Queries both the raw ledger and the hourly aggregates, combining
        their totals so the caller always sees the full picture regardless
        of compression state.

        Args:
            cluster_name: Cluster identifier used for attribution.
            group_by_method: When True, group by ``measurement_method``
                (``prorated`` / ``measured``) instead of recommendation type.

        Returns:
            ``{group_key: {"co2e_saved_grams": float, "cost_saved_dollars": float}}``
        """

    @abstractmethod
    async def get_window_totals(
        self,
        cluster_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str | None = None,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Return exact savings grouped by recommendation_type for a time window.

        Queries both raw ledger rows and hourly aggregates so callers get the
        same totals regardless of whether older savings records were compressed.
        When ``namespace`` is provided, only savings attributed to that namespace
        are included. An empty namespace string selects cluster-scoped rows that
        have no namespace attribution.

        Args:
            cluster_name: Cluster identifier used for attribution.
            start_time: Inclusive window start.
            end_time: Inclusive window end.
            namespace: Optional namespace filter.
            group_by_method: When True, group by ``measurement_method`` instead
                of recommendation type.

        Returns:
            ``{group_key: {"co2e_saved_grams": float, "cost_saved_dollars": float}}``
        """

    @abstractmethod
    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded.

        Called when a recommendation enters rollback review or is reverted so
        its prior savings are excluded without deleting history.

        Returns:
            Number of rows flagged.
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
