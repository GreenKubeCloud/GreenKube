# src/greenkube/storage/base_savings_repository.py
"""Abstract interface for the recommendation savings ledger repository.

The ledger is append-only: accrual rows are inserted once per recommendation,
method and UTC slot and are never compressed, overwritten or deleted. The only
mutation is the ``superseded`` flag set when a later outcome invalidates past
accruals. Every read goes through the ``savings_accruals_v1`` view, which
also exposes the ``legacy_compressed`` hourly rows left by releases before 0.4.
"""

from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from ..models.savings import SavingsAccrualTotal, SavingsCounterTotal, SavingsLedgerRecord

ATTRIBUTION_STREAM = "recommendation_savings"


class AccrualWatermarkConflict(RuntimeError):
    """Raised when another writer advanced the accrual watermark first."""


class SavingsLedgerRepository(ABC):
    """Port for persisting and querying the recommendation savings ledger."""

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @abstractmethod
    async def save_records(self, records: List[SavingsLedgerRecord]) -> int:
        """Append accrual rows, ignoring rows whose idempotency key already exists.

        The key is ``(recommendation_id, measurement_method, slot_start)`` for
        grid accruals and ``(recommendation_id, period_start, period_end,
        measurement_method)`` otherwise. Existing rows are never updated.

        Returns:
            Number of rows actually inserted.
        """

    @abstractmethod
    async def append_accruals(
        self,
        records: List[SavingsLedgerRecord],
        *,
        cluster_name: str,
        stream: str,
        expected_watermark: Optional[datetime],
        new_watermark: datetime,
    ) -> int:
        """Atomically advance the accrual watermark and append ``records``.

        The watermark moves from ``expected_watermark`` (``None`` when absent)
        to ``new_watermark`` with a compare-and-swap in the same transaction as
        the inserts, so two writers can never both accrue the same slots.

        Raises:
            AccrualWatermarkConflict: another writer moved the watermark first;
                nothing is written.

        Returns:
            Number of rows inserted.
        """

    @abstractmethod
    async def supersede_for_recommendation(self, recommendation_id: int) -> int:
        """Flags every attribution row for a recommendation as superseded.

        Called when a recommendation enters rollback review or is reverted so
        its prior savings are excluded without deleting history.

        Returns:
            Number of rows flagged.
        """

    # ------------------------------------------------------------------
    # Watermarks
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_accrual_watermark(self, cluster_name: str, stream: str = ATTRIBUTION_STREAM) -> Optional[datetime]:
        """Return the end of the last accrued slot for a stream, or ``None``."""

    @abstractmethod
    async def get_accrual_floors(self, recommendation_ids: Iterable[int]) -> Dict[int, datetime]:
        """Return the latest ``period_end`` already in the ledger per recommendation.

        Accrual never starts before this instant, so rows written by earlier
        releases (wall-clock periods, legacy hourly rows) are never re-accrued.
        """

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_accrual_breakdown(
        self,
        cluster_name: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        namespace: Optional[str] = None,
        recommendation_ids: Optional[Sequence[int]] = None,
    ) -> List[SavingsAccrualTotal]:
        """Return net accruals grouped by namespace, recommendation type and method.

        ``recommendation_ids`` restricts the rows to those recommendations (MEA-16).

        Superseded rows are excluded. With a window, each row contributes the
        share of its period that overlaps ``[start_time, end_time)``, so a
        window that starts in the middle of a slot (or of a legacy hourly
        bucket) includes that partial slot. Without a window, all rows count.
        ``namespace=""`` selects cluster-scoped rows.
        """

    @abstractmethod
    async def get_lifetime_counters(self, cluster_name: str) -> List[SavingsCounterTotal]:
        """Return monotonic accrued / reversed totals per recommendation type and method.

        ``accrued`` sums positive accruals; ``reversed`` sums negative accruals
        (negative declarations or signed projections) plus every superseded
        accrual, so ``accrued - reversed`` is the net non-superseded total.
        """

    @abstractmethod
    async def has_legacy_compressed_rows(
        self,
        cluster_name: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        namespace: Optional[str] = None,
    ) -> bool:
        """Return True when legacy compressed hourly rows overlap the window.

        ``namespace`` narrows the check like :meth:`get_accrual_breakdown`
        (``""`` selects cluster-scoped rows).
        """

    # ------------------------------------------------------------------
    # Convenience aggregations (backend independent)
    # ------------------------------------------------------------------

    async def get_window_totals(
        self,
        cluster_name: str,
        start_time: datetime,
        end_time: datetime,
        namespace: str | None = None,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Return net savings for a window grouped by recommendation type or method.

        Rows partially inside the window contribute their overlapping share.
        Grouping by type sums across methods and is therefore only meant for
        callers that already filtered a single method; reporting code should
        use :meth:`get_accrual_breakdown`.
        """
        breakdown = await self.get_accrual_breakdown(cluster_name, start_time, end_time, namespace=namespace)
        return _group_totals(breakdown, group_by_method)

    async def get_cumulative_totals(
        self,
        cluster_name: str,
        group_by_method: bool = False,
    ) -> Dict[str, Dict[str, float]]:
        """Return all-time net savings grouped by recommendation type or method."""
        breakdown = await self.get_accrual_breakdown(cluster_name)
        return _group_totals(breakdown, group_by_method)


def _group_totals(breakdown: List[SavingsAccrualTotal], group_by_method: bool) -> Dict[str, Dict[str, float]]:
    result: Dict[str, Dict[str, float]] = defaultdict(lambda: {"co2e_saved_grams": 0.0, "cost_saved_dollars": 0.0})
    for row in breakdown:
        key = row.method if group_by_method else row.recommendation_type
        result[key]["co2e_saved_grams"] += row.co2e_saved_grams
        result[key]["cost_saved_dollars"] += row.cost_saved_dollars
    return dict(result)
