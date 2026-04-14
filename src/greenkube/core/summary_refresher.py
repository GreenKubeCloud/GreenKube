# src/greenkube/core/summary_refresher.py
"""
Dashboard summary refresher.

Computes aggregated totals and time-series buckets for a small set of
fixed time windows and persists them in two pre-computed tables:

* ``metrics_summary``           – scalar KPI totals per window
* ``metrics_timeseries_cache``  – ordered time buckets for charts

Both tables are refreshed hourly by the background scheduler so the
frontend can load KPI cards and charts instantly without scanning millions
of raw metric rows, eliminating OOM errors.

Windows and granularity
-----------------------
* ``24h``  – last 24 hours   → hourly  buckets  (≤ 24 rows)
* ``7d``   – last 7 days     → daily   buckets  (7 rows)
* ``30d``  – last 30 days    → daily   buckets  (30 rows)
* ``1y``   – last 365 days   → weekly  buckets  (≤ 53 rows)
* ``ytd``  – since Jan 1st   → monthly buckets  (≤ 12 rows)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..models.metrics import MetricsSummaryRow, TimeseriesCachePoint
from ..storage.base_repository import (
    CombinedMetricsRepository,
    SummaryRepository,
    TimeseriesCacheRepository,
)

logger = logging.getLogger(__name__)

# (slug, timedelta | None, granularity)
# timedelta=None means "year-to-date" (start computed dynamically).
_WINDOWS: List[tuple] = [
    ("24h", timedelta(hours=24), "hour"),
    ("7d", timedelta(days=7), "day"),
    ("30d", timedelta(days=30), "day"),
    ("1y", timedelta(days=365), "week"),
    ("ytd", None, "month"),
]


def _ytd_start(now: datetime) -> datetime:
    """Return UTC midnight on 1 January of *now*'s year."""
    return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


class SummaryRefresher:
    """Recomputes and persists all dashboard summary and timeseries cache rows.

    Parameters
    ----------
    metrics_repo:
        Repository used to run aggregation queries against raw/hourly tables.
    summary_repo:
        Repository used to persist the computed KPI scalar rows.
    timeseries_cache_repo:
        Repository used to persist the computed timeseries bucket rows.
    namespaces:
        Optional explicit list of namespaces to also compute per-namespace
        data for.  When ``None`` the refresher discovers namespaces via
        ``metrics_repo.list_namespaces()``.
    """

    def __init__(
        self,
        metrics_repo: CombinedMetricsRepository,
        summary_repo: SummaryRepository,
        timeseries_cache_repo: TimeseriesCacheRepository,
        namespaces: Optional[List[str]] = None,
    ) -> None:
        self._metrics = metrics_repo
        self._summary = summary_repo
        self._ts_cache = timeseries_cache_repo
        self._namespaces = namespaces

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Refresh all summary and timeseries cache rows.

        Returns:
            The total number of rows upserted across both tables.
        """
        now = datetime.now(timezone.utc)
        count = 0

        # 1. Cluster-wide rows
        count += await self._refresh_for_namespace(now, namespace=None)

        # 2. Per-namespace rows
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
        for slug, delta, granularity in _WINDOWS:
            start = _ytd_start(now) if delta is None else now - delta
            try:
                count += await self._refresh_summary_row(slug, start, now, namespace)
            except Exception as exc:
                logger.error(
                    "Failed to refresh summary for slug='%s' namespace='%s': %s",
                    slug,
                    namespace,
                    exc,
                )
            try:
                count += await self._refresh_timeseries_points(slug, granularity, start, now, namespace)
            except Exception as exc:
                logger.error(
                    "Failed to refresh timeseries for slug='%s' namespace='%s': %s",
                    slug,
                    namespace,
                    exc,
                )
        return count

    async def _refresh_summary_row(
        self,
        slug: str,
        start: datetime,
        now: datetime,
        namespace: Optional[str],
    ) -> int:
        """Compute and upsert one KPI scalar row. Returns 1 on success."""
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
        return 1

    async def _refresh_timeseries_points(
        self,
        slug: str,
        granularity: str,
        start: datetime,
        now: datetime,
        namespace: Optional[str],
    ) -> int:
        """Compute and upsert all timeseries buckets for one window. Returns bucket count."""
        raw_rows = await self._metrics.aggregate_timeseries(
            start_time=start,
            end_time=now,
            granularity=granularity,
            namespace=namespace,
        )
        points = [
            TimeseriesCachePoint(
                window_slug=slug,
                namespace=namespace,
                bucket_ts=row["timestamp"],
                co2e_grams=row.get("co2e_grams", 0.0),
                embodied_co2e_grams=row.get("embodied_co2e_grams", 0.0),
                total_cost=row.get("total_cost", 0.0),
                joules=row.get("energy_joules", 0.0),
            )
            for row in raw_rows
        ]
        await self._ts_cache.upsert_points(points)
        return len(points)
