# src/greenkube/collectors/electricity_maps_collector.py
import requests

from ..core.config import config
from .base_collector import BaseCollector

API_BASE_URL = "https://api.electricitymaps.com/v3"


class ElectricityMapsCollector(BaseCollector):
    """
    Un collecteur pour récupérer les données d'intensité carbone depuis l'API d'Electricity Maps.
    Il ne gère plus la sauvegarde en base de données.
    """

    def __init__(self):
        """
        Initialise le collecteur. L'argument 'zone' n'est plus nécessaire ici.
        """
        if not config.ELECTRICITY_MAPS_TOKEN:
            raise ValueError("ELECTRICITY_MAPS_TOKEN is not set in the environment.")
        self.api_token = config.ELECTRICITY_MAPS_TOKEN
        self.headers = {"auth-token": self.api_token}

    def collect(self, zone: str) -> list:
        """
        Récupère les données historiques pour une zone spécifique et les retourne.
        """
        history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={zone}"
        print(f"Fetching carbon intensity history for zone: {zone}...")

        try:
            response = requests.get(history_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("history", [])
        except requests.exceptions.RequestException as e:
            print(f"Error fetching data from Electricity Maps API: {e}")
            return []  # Retourne une liste vide en cas d'erreur
