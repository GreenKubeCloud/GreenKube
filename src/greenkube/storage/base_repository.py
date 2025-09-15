# src/greenkube/storage/base_repository.py
from abc import ABC, abstractmethod

class CarbonIntensityRepository(ABC):
    """
    Interface définissant le contrat pour le stockage et la récupération
    des données d'intensité carbone.
    """
    @abstractmethod
    def get_latest_for_zone(self, zone: str) -> float | None:
        """Récupère la dernière intensité carbone pour une zone."""
        pass

    @abstractmethod
    def save_history(self, data: list, zone: str):
        """Sauvegarde les données historiques pour une zone."""
        pass