# src/greenkube/collectors/electricity_maps_collector.py
import logging
from datetime import datetime, timezone

import requests

from ..core.config import config
from ..data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE
from ..utils.http_client import get_http_session
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
        self.session = get_http_session()
        if not config.ELECTRICITY_MAPS_TOKEN:
            logger.warning("ELECTRICITY_MAPS_TOKEN is not set in the environment. Using default values.")
            self.api_token = None
        else:
            self.api_token = config.ELECTRICITY_MAPS_TOKEN
            self.headers = {"auth-token": self.api_token}

    def collect(self, zone: str, target_datetime: datetime = None) -> list:
        """
        Retrieves historical data for a specific zone and returns it.
        If the token is missing or the API call fails, it returns the default value.
        """
        if self.api_token:
            history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={zone}"
            logger.info(f"Fetching carbon intensity history for zone: {zone}...")

            try:
                # Use the robust session with timeouts and retries
                response = self.session.get(history_url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                return data.get("history", [])
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching data from Electricity Maps API: {e}")
                # Fallback to default values below

        # Fallback to default values
        logger.info(f"Using default grid intensity for zone: {zone}")
        default_intensity = DEFAULT_GRID_INTENSITY_BY_ZONE.get(zone)
        if default_intensity is not None:
            if target_datetime:
                # Ensure target_datetime is aware (UTC) for isoformat
                if target_datetime.tzinfo is None:
                    target_datetime = target_datetime.replace(tzinfo=timezone.utc)
                dt_iso = target_datetime.isoformat()
            else:
                dt_iso = datetime.now(timezone.utc).isoformat()

            return [
                {
                    "carbonIntensity": default_intensity,
                    "datetime": dt_iso,
                    "zone": zone,
                    "isEstimated": True,
                    "estimationMethod": "default_fallback",
                }
            ]

        logger.warning(f"No default grid intensity found for zone: {zone}")
        return []
