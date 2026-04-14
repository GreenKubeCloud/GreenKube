# src/greenkube/core/summary_refresher.py
"""
Dashboard summary refresher.

Computes aggregated totals for a small set of fixed time windows and
persists them in the ``metrics_summary`` table via :class:`SummaryRepository`.

The result is a handful of rows (< 10 per namespace) that the frontend
can query instantly without performing any heavy aggregation at request
time, eliminating OOM errors caused by loading all raw metric rows.

Windows computed
----------------
* ``24h``  – last 24 hours
* ``7d``   – last 7 days
* ``30d``  – last 30 days
* ``1y``   – last 365 days
* ``ytd``  – since 1 January of the current year (UTC)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..models.metrics import MetricsSummaryRow
from ..storage.base_repository import CombinedMetricsRepository, SummaryRepository

logger = logging.getLogger(__name__)

# Fixed windows: (slug, timedelta | None).
# None means "year-to-date" (computed dynamically at runtime).
_WINDOWS: List[tuple] = [
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
    ("30d", timedelta(days=30)),
    ("1y", timedelta(days=365)),
    ("ytd", None),  # computed at call time
]


def _ytd_start(now: datetime) -> datetime:
    """Return UTC midnight on 1 January of *now*'s year."""
    return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


class SummaryRefresher:
    """Recomputes and persists all dashboard summary rows.

    Parameters
    ----------
    metrics_repo:
        Repository used to run aggregation queries against raw/hourly tables.
    summary_repo:
        Repository used to persist the computed rows.
    namespaces:
        Optional explicit list of namespaces to also compute per-namespace
        summaries for.  When ``None`` the refresher skips per-namespace rows
        and only computes cluster-wide totals.
    """

    def __init__(
        self,
        metrics_repo: CombinedMetricsRepository,
        summary_repo: SummaryRepository,
        namespaces: Optional[List[str]] = None,
    ) -> None:
        self._metrics = metrics_repo
        self._summary = summary_repo
        self._namespaces = namespaces

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Refresh all summary rows.

        Returns:
            The number of rows upserted.
        """
        now = datetime.now(timezone.utc)
        count = 0

        # 1. Cluster-wide rows
        count += await self._refresh_for_namespace(now, namespace=None)

        # 2. Per-namespace rows (when namespace list is available)
        if self._namespaces is None:
            try:
                self._namespaces = await self._metrics.list_namespaces()
            except Exception as exc:
                logger.warning("Could not list namespaces for summary refresh: %s", exc)
                self._namespaces = []

        for ns in self._namespaces:
            try:
                count += await self._refresh_for_namespace(now, namespace=ns)
            except Exception as exc:
                logger.error("Summary refresh failed for namespace '%s': %s", ns, exc)

        logger.info("SummaryRefresher: upserted %d rows.", count)
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_for_namespace(self, now: datetime, namespace: Optional[str]) -> int:
        """Compute and upsert all window rows for a single namespace (or cluster)."""
        count = 0
        for slug, delta in _WINDOWS:
            start = _ytd_start(now) if delta is None else now - delta
            try:
                agg = await self._metrics.aggregate_summary(
                    start_time=start,
                    end_time=now,
                    namespace=namespace,
                )
                row = MetricsSummaryRow(
                    window_slug=slug,
                    namespace=namespace,
                    total_co2e_grams=agg.get("total_co2e_grams", 0.0),
                    total_embodied_co2e_grams=agg.get("total_embodied_co2e_grams", 0.0),
                    total_cost=agg.get("total_cost", 0.0),
                    total_energy_joules=agg.get("total_energy_joules", 0.0),
                    pod_count=agg.get("pod_count", 0),
                    namespace_count=agg.get("namespace_count", 0),
                    updated_at=now,
                )
                await self._summary.upsert_row(row)
                count += 1
            except Exception as exc:
                logger.error(
                    "Failed to refresh summary for slug='%s' namespace='%s': %s",
                    slug,
                    namespace,
                    exc,
                )
        return count
