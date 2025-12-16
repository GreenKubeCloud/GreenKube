# src/greenkube/storage/base_repository.py
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from ..models.metrics import CombinedMetric
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

    @abstractmethod
    async def write_combined_metrics(self, metrics: List[CombinedMetric]):
        """
        Writes a list of CombinedMetric objects to the repository.
        """
        pass

    @abstractmethod
    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        """
        Reads CombinedMetric objects from the repository within a given time range.
        """
        pass
