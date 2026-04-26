# src/greenkube/core/savings_attributor.py
"""
Savings attribution service.

Prorates annual savings estimates from applied recommendations into
per-collection-period time-series records.  These records are stored
in the ``recommendation_savings_ledger`` table and exposed as DB-backed
Prometheus gauges so Grafana can use ``increase(metric[$__range])``
to display the actual savings for any selected time window.

Design:
    annual_co2e / 8760 h × (period_seconds / 3600) = co2e per period
    annual_cost / 8760 h × (period_seconds / 3600) = cost per period

This is a prorated estimate — the most accurate approach available
without per-before/after measurement infrastructure.  It is correct
for recommendations that have a stable, ongoing effect (e.g. node
right-sizing that stays in place).
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List

from ..models.metrics import RecommendationRecord
from ..models.savings import SavingsLedgerRecord
from ..storage.base_savings_repository import SavingsLedgerRepository

logger = logging.getLogger(__name__)

# Seconds in a year — basis for the proration formula.
_SECONDS_PER_YEAR: float = 365.25 * 24 * 3600


class SavingsAttributor:
    """Attributes prorated per-period savings to applied recommendations."""

    def __init__(
        self,
        savings_repo: SavingsLedgerRepository,
        cluster_name: str,
    ) -> None:
        self._repo = savings_repo
        self._cluster = cluster_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _compute_period_records(
        self,
        applied_records: List[RecommendationRecord],
        period_seconds: int,
    ) -> List[SavingsLedgerRecord]:
        """Compute the savings records for a single collection period.

        Only recommendations with a positive ``carbon_saved_co2e_grams``
        value are included — those without an estimate contribute zero and
        are skipped to keep the ledger clean.

        Args:
            applied_records: Applied RecommendationRecord objects from the DB.
            period_seconds:  Length of the current collection period in seconds.

        Returns:
            List of SavingsLedgerRecord ready for persistence.
        """
        now = datetime.now(timezone.utc)
        records: List[SavingsLedgerRecord] = []

        for rec in applied_records:
            annual_co2e = rec.carbon_saved_co2e_grams
            annual_cost = rec.cost_saved

            # Skip if there is no positive annual CO₂ estimate.
            if not annual_co2e or annual_co2e <= 0:
                continue

            factor = period_seconds / _SECONDS_PER_YEAR
            rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)

            records.append(
                SavingsLedgerRecord(
                    recommendation_id=rec.id,
                    cluster_name=self._cluster,
                    namespace=rec.namespace or "",
                    recommendation_type=rec_type,
                    co2e_saved_grams=annual_co2e * factor,
                    cost_saved_dollars=(annual_cost or 0.0) * factor,
                    period_seconds=period_seconds,
                    timestamp=now,
                )
            )

        return records

    async def attribute_period(
        self,
        applied_records: List[RecommendationRecord],
        period_seconds: int,
    ) -> int:
        """Compute and persist savings for the current collection period.

        Errors are caught and logged; the caller's collection loop is never
        interrupted by a savings attribution failure.

        Args:
            applied_records: Applied recommendations from the repository.
            period_seconds:  Length of the current collection period.

        Returns:
            Number of records written (0 on error or nothing to write).
        """
        records = self._compute_period_records(applied_records, period_seconds)
        if not records:
            return 0
        try:
            return await self._repo.save_records(records)
        except Exception as exc:
            logger.error("SavingsAttributor: failed to save period records: %s", exc)
            return 0

    async def get_cumulative_totals(
        self,
    ) -> Dict[str, Dict[str, float]]:
        """Return cumulative savings by recommendation_type for this cluster.

        Returns:
            ``{rec_type: {"co2e_saved_grams": float, "cost_saved_dollars": float}}``
        """
        return await self._repo.get_cumulative_totals(cluster_name=self._cluster)
