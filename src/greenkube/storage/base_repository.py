# src/greenkube/storage/base_repository.py
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..models.metrics import CombinedMetric, RecommendationRecord
from ..models.node import NodeInfo


class NodeRepository(ABC):
    """
    Abstract base class for node data repositories.
    Defines the contract for saving and retrieving node snapshots.
    """

    @abstractmethod
    async def save_nodes(self, nodes: List[NodeInfo]) -> int:
        """
        Saves node snapshots to the repository.

        Args:
            nodes: A list of NodeInfo objects to save.

        Returns:
            The number of records saved.
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
