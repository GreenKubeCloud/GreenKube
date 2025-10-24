# src/greenkube/storage/base_repository.py
from abc import ABC, abstractmethod

class CarbonIntensityRepository(ABC):
    """
    Abstract base class for carbon intensity data repositories.
    Defines the contract for saving and retrieving carbon intensity data.
    """
    
    @abstractmethod
    def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
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
    def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data for a specific zone.
        
        Args:
            history_data: A list of dictionaries containing carbon intensity records.
            zone: The zone for which to save the data.
            
        Returns:
            The number of new records saved.
        """
        pass

