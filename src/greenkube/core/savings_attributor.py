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
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from ..models.metrics import RecommendationRecord
from ..models.savings import SavingsLedgerRecord
from ..storage.base_savings_repository import SavingsLedgerRepository
from ..utils.date_utils import ensure_utc

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
        period_seconds: int | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> List[SavingsLedgerRecord]:
        """Compute the savings records for a single collection period.

        Verified recommendations contribute their measured savings
        (``measurement_method='measured'``); everything else contributes the
        prorated projection. Recommendations in rollback review or reverted are
        excluded so attribution stops at the rollback event. A record is written
        when either the CO2e or the cost value is positive.

        Args:
            applied_records: Applied RecommendationRecord objects from the DB.
            period_seconds:  Length of the current collection period in seconds.

        Returns:
            List of SavingsLedgerRecord ready for persistence.
        """
        if period_start is None and period_end is None:
            period_end = datetime.now(timezone.utc)
            period_start = period_end - timedelta(seconds=period_seconds or 300)
        elif period_start is None or period_end is None:
            raise ValueError("period_start and period_end must be provided together")
        period_start = ensure_utc(period_start)
        period_end = ensure_utc(period_end)
        period_seconds = int((period_end - period_start).total_seconds())
        if period_seconds <= 0:
            raise ValueError("period_end must be after period_start")
        now = period_end
        records: List[SavingsLedgerRecord] = []

        for rec in applied_records:
            status = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
            if status in ("rollback_review", "reverted"):
                continue

            is_measured = status == "verified" and rec.measured_co2e_saved_grams is not None
            if is_measured:
                annual_co2e = rec.measured_co2e_saved_grams
                annual_cost = rec.measured_cost_saved or 0.0
                method = "measured"
            else:
                annual_co2e = rec.carbon_saved_co2e_grams
                annual_cost = rec.cost_saved
                method = "prorated"

            # Skip only when there is nothing positive to attribute. A verified
            # recommendation with zero measured CO2e can still have a real cost
            # saving, and vice versa.
            has_co2 = bool(annual_co2e and annual_co2e > 0)
            has_cost = bool(annual_cost and annual_cost > 0)
            if not has_co2 and not has_cost:
                continue

            # Skip records without a database ID (not yet persisted).
            if rec.id is None:
                continue

            factor = period_seconds / _SECONDS_PER_YEAR
            rec_type = rec.type.value if hasattr(rec.type, "value") else str(rec.type)

            records.append(
                SavingsLedgerRecord(
                    recommendation_id=rec.id,
                    cluster_name=self._cluster,
                    namespace=rec.namespace or "",
                    recommendation_type=rec_type,
                    co2e_saved_grams=(annual_co2e or 0.0) * factor,
                    cost_saved_dollars=(annual_cost or 0.0) * factor,
                    period_seconds=period_seconds,
                    timestamp=now,
                    period_start=period_start,
                    period_end=period_end,
                    measurement_method=method,
                    baseline_value=rec.potential_savings_co2e_grams,
                    actual_value=annual_co2e,
                    confidence=rec.confidence,
                )
            )

        return records

    async def attribute_period(
        self,
        applied_records: List[RecommendationRecord],
        period_seconds: int | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
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
        records = self._compute_period_records(
            applied_records,
            period_seconds=period_seconds,
            period_start=period_start,
            period_end=period_end,
        )
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
