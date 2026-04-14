# src/greenkube/storage/base_repository.py
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..models.metrics import CombinedMetric, MetricsSummaryRow, RecommendationRecord, TimeseriesCachePoint
from ..models.node import NodeInfo


class NodeRepository(ABC):
    """
    Abstract base class for node data repositories.
    Defines the contract for saving and retrieving node snapshots.
    Supports SCD Type 2 pattern: only stores a new record when node
    configuration actually changes.
    """

    @abstractmethod
    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots using SCD Type 2 logic.
        Only inserts a new record when a node's configuration changes.

        Args:
            nodes: A list of NodeInfo objects to save.

        Returns:
            The number of new records created.
        """
        pass

    @abstractmethod
    async def get_snapshots(self, start: datetime, end: datetime) -> List[tuple[str, NodeInfo]]:
        """
        Retrieves node snapshots within a time range.

        Args:
            start: Start datetime (inclusive).
            end: End datetime (inclusive).

        Returns:
            A list of tuples (timestamp_str, NodeInfo).
        """
        pass

    @abstractmethod
    async def get_latest_snapshots_before(self, timestamp: datetime) -> List[NodeInfo]:
        """
        Retrieves the latest snapshot for each node before the given timestamp.

        Args:
            timestamp: The cutoff timestamp.

        Returns:
            A list of NodeInfo objects.
        """
        pass


class CarbonIntensityRepository(ABC):
    """
    Abstract base class for carbon intensity data repositories.
    Defines the contract for saving and retrieving carbon intensity data.
    """

    @abstractmethod
    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.

        Args:
            zone: The geographical zone (e.g., 'FR').
            timestamp: The ISO 8601 timestamp to query against.

        Returns:
            The carbon intensity value, or None if not found.
        """
        pass

    @abstractmethod
    async def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data for a specific zone.

        Args:
            history_data: A list of dictionaries containing carbon intensity records.
            zone: The zone for which to save the data.

        Returns:
            The number of new records saved.
        """
        pass


class CombinedMetricsRepository(ABC):
    """
    Abstract base class for combined metrics data repositories.
    Defines the contract for writing and reading CombinedMetric records.
    """

    @abstractmethod
    async def write_combined_metrics(self, metrics: List[CombinedMetric]) -> int:
        """
        Writes a list of CombinedMetric objects to the repository.

        Args:
            metrics: A list of CombinedMetric objects to persist.

        Returns:
            The number of records saved.
        """
        pass

    @abstractmethod
    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        """
        Reads CombinedMetric objects from the repository within a given time range.

        Args:
            start_time: Start datetime (inclusive).
            end_time: End datetime (inclusive).

        Returns:
            A list of CombinedMetric objects.
        """
        pass

    async def read_hourly_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        """Read from the pre-aggregated hourly table.

        Default implementation falls back to read_combined_metrics.
        Subclasses should override for efficient SQL-level access.
        """
        metrics = await self.read_combined_metrics(start_time, end_time)
        if namespace:
            metrics = [m for m in metrics if m.namespace == namespace]
        return metrics

    async def read_combined_metrics_smart(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        """Intelligently read from raw or hourly table based on time range.

        For recent data (within METRICS_COMPRESSION_AGE_HOURS), reads raw data.
        For older data, reads from the hourly table.
        For mixed ranges, merges both sources.
        """
        from datetime import timedelta
        from datetime import timezone as tz

        from ..core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        compression_cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        # Ensure timezone-aware comparison
        start_aware = start_time if start_time.tzinfo else start_time.replace(tzinfo=tz.utc)
        end_aware = end_time if end_time.tzinfo else end_time.replace(tzinfo=tz.utc)

        # Use a 1-minute tolerance to avoid split-brain when the requested
        # start is effectively at the compression boundary (e.g. "last 24h").
        if start_aware >= compression_cutoff - timedelta(minutes=1):
            # All data is recent — use raw table
            metrics = await self.read_combined_metrics(start_time, end_time)
            if namespace:
                metrics = [m for m in metrics if m.namespace == namespace]
            return metrics

        if end_aware <= compression_cutoff:
            # All data is old — use hourly table
            return await self.read_hourly_metrics(start_time, end_time, namespace)

        # Mixed range: hourly for old part, raw for recent part
        hourly_metrics = await self.read_hourly_metrics(start_time, compression_cutoff, namespace)
        raw_metrics = await self.read_combined_metrics(compression_cutoff, end_time)
        if namespace:
            raw_metrics = [m for m in raw_metrics if m.namespace == namespace]
        return hourly_metrics + raw_metrics

    async def list_namespaces(self) -> List[str]:
        """Return a sorted list of known namespaces from the cache.

        Default implementation falls back to scanning recent metrics.
        """
        from datetime import timedelta
        from datetime import timezone as tz

        end = datetime.now(tz.utc)
        start = end - timedelta(days=7)
        metrics = await self.read_combined_metrics(start, end)
        return sorted({m.namespace for m in metrics})

    async def aggregate_summary(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> dict:
        """
        Returns aggregated summary metrics (totals + counts) for the time range.

        Default implementation loads all rows and aggregates in Python.
        Subclasses may override with a SQL-level implementation for better performance.

        Returns:
            A dict with keys: total_co2e_grams, total_embodied_co2e_grams, total_cost,
            total_energy_joules, pod_count, namespace_count.
        """
        metrics = await self.read_combined_metrics(start_time, end_time)
        if namespace:
            metrics = [m for m in metrics if m.namespace == namespace]
        return {
            "total_co2e_grams": sum(m.co2e_grams for m in metrics),
            "total_embodied_co2e_grams": sum(m.embodied_co2e_grams or 0.0 for m in metrics),
            "total_cost": sum(m.total_cost for m in metrics),
            "total_energy_joules": sum(m.joules for m in metrics),
            "pod_count": len({m.pod_name for m in metrics}),
            "namespace_count": len({m.namespace for m in metrics}),
        }

    async def aggregate_timeseries(
        self,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "hour",
        namespace: Optional[str] = None,
    ) -> List[dict]:
        """
        Returns time-series data bucketed by granularity.

        Default implementation loads all rows and aggregates in Python.
        Subclasses may override with a SQL-level implementation for better performance.

        Returns:
            List of dicts with keys: timestamp, co2e_grams, embodied_co2e_grams, total_cost,
            energy_joules, cpu_usage_millicores, memory_usage_bytes.
        """
        from collections import defaultdict

        _GRANULARITY_FORMATS = {
            "hour": "%Y-%m-%dT%H:00:00Z",
            "day": "%Y-%m-%dT00:00:00Z",
            "week": "%Y-W%V",
            "month": "%Y-%m-01T00:00:00Z",
        }
        fmt = _GRANULARITY_FORMATS.get(granularity, "%Y-%m-%dT%H:00:00Z")

        metrics = await self.read_combined_metrics(start_time, end_time)
        if namespace:
            metrics = [m for m in metrics if m.namespace == namespace]

        buckets: dict[str, list] = defaultdict(list)
        for m in metrics:
            if m.timestamp:
                key = m.timestamp.strftime(fmt)
                buckets[key].append(m)

        result = []
        for ts_key in sorted(buckets):
            bucket = buckets[ts_key]
            result.append(
                {
                    "timestamp": ts_key,
                    "co2e_grams": sum(m.co2e_grams for m in bucket),
                    "embodied_co2e_grams": sum(m.embodied_co2e_grams or 0.0 for m in bucket),
                    "total_cost": sum(m.total_cost for m in bucket),
                    "energy_joules": sum(m.joules for m in bucket),
                    "cpu_usage_millicores": sum(m.cpu_usage_millicores or 0 for m in bucket),
                    "memory_usage_bytes": sum(m.memory_usage_bytes or 0 for m in bucket),
                }
            )
        return result


class RecommendationRepository(ABC):
    """
    Abstract base class for recommendation history repositories.
    Defines the contract for saving and retrieving recommendation snapshots.
    """

    @abstractmethod
    async def save_recommendations(self, records: List[RecommendationRecord]) -> int:
        """
        Saves recommendation records to the repository.

        Args:
            records: A list of RecommendationRecord objects to persist.

        Returns:
            The number of records saved.
        """
        pass

    @abstractmethod
    async def get_recommendations(
        self,
        start: datetime,
        end: datetime,
        rec_type: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> List[RecommendationRecord]:
        """
        Retrieves recommendation records within a time range.

        Args:
            start: Start datetime (inclusive).
            end: End datetime (inclusive).
            rec_type: Optional filter by recommendation type.
            namespace: Optional filter by namespace.

        Returns:
            A list of RecommendationRecord objects.
        """
        pass


class SummaryRepository(ABC):
    """
    Abstract base class for the pre-computed dashboard summary table.

    The ``metrics_summary`` table holds one row per (window_slug, namespace)
    pair.  It is refreshed hourly by
    :class:`~greenkube.core.summary_refresher.SummaryRefresher` so the
    frontend can retrieve KPI data with a single lightweight query.
    """

    # Built-in window slugs that are always computed cluster-wide.
    BUILTIN_SLUGS: List[str] = ["24h", "7d", "30d", "1y", "ytd"]

    @abstractmethod
    async def upsert_row(self, row: MetricsSummaryRow) -> None:
        """
        Inserts or updates a summary row identified by (window_slug, namespace).

        Args:
            row: The pre-computed :class:`MetricsSummaryRow` to persist.
        """
        pass

    @abstractmethod
    async def get_rows(self, namespace: Optional[str] = None) -> List[MetricsSummaryRow]:
        """
        Returns all summary rows, optionally filtered by namespace.

        Pass ``namespace=None`` to retrieve only cluster-wide rows
        (where the stored namespace IS NULL).

        Args:
            namespace: If provided, return only rows for that namespace.
                       If ``None``, return only cluster-wide rows.

        Returns:
            A list of :class:`MetricsSummaryRow` objects.
        """
        pass


class TimeseriesCacheRepository(ABC):
    """
    Abstract base class for the pre-computed timeseries chart cache.

    The ``metrics_timeseries_cache`` table holds one row per
    (window_slug, namespace, bucket_ts) triple.  It is refreshed hourly
    alongside :class:`SummaryRepository` so the frontend can render
    time-series charts without scanning millions of raw metric rows.

    Granularity per window:
        - ``24h``  → hourly buckets  (≤24 rows)
        - ``7d``   → daily  buckets  (7 rows)
        - ``30d``  → daily  buckets  (30 rows)
        - ``1y``   → daily  buckets  (365 rows)
        - ``ytd``  → daily  buckets  (≤366 rows)
    """

    @abstractmethod
    async def upsert_points(self, points: List[TimeseriesCachePoint]) -> None:
        """Replace all cached points for a (window_slug, namespace) pair.

        The caller passes the complete, freshly-computed list for one
        window+namespace combination.  The implementation should delete
        existing rows for that pair then insert the new ones, keeping
        the table consistent without requiring explicit deletes elsewhere.

        Args:
            points: The new time-series cache points for one window+namespace.
        """
        pass

    @abstractmethod
    async def get_points(
        self,
        window_slug: str,
        namespace: Optional[str] = None,
    ) -> List[TimeseriesCachePoint]:
        """Return all cached points for a given window slug and namespace.

        Args:
            window_slug: The time window identifier (e.g. ``'7d'``).
            namespace: Namespace filter; ``None`` returns cluster-wide points.

        Returns:
            An ordered list of :class:`TimeseriesCachePoint` objects.
        """
        pass
