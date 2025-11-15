# src/greenkube/collectors/electricity_maps_collector.py
import logging

import requests

from ..core.config import config
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.electricitymaps.com/v3"


class ElectricityMapsCollector(BaseCollector):
    """
    A collector to retrieve carbon intensity data from the Electricity Maps API.
    It no longer handles saving to the database.
    """

    def __init__(self):
        """
        Initializes the collector. The 'zone' argument is no longer needed here.
        """
        if not config.ELECTRICITY_MAPS_TOKEN:
            raise ValueError("ELECTRICITY_MAPS_TOKEN is not set in the environment.")
        self.api_token = config.ELECTRICITY_MAPS_TOKEN
        self.headers = {"auth-token": self.api_token}

    def collect(self, zone: str) -> list:
        """
        Retrieves historical data for a specific zone and returns it.
        """
        history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={zone}"
        logger.info(f"Fetching carbon intensity history for zone: {zone}...")

        try:
            response = requests.get(history_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("history", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data from Electricity Maps API: {e}")
            return []  # Returns an empty list in case of an error
