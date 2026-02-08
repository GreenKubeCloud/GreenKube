import logging
from datetime import datetime, timezone

import httpx

from ..core.config import config
from ..data.electricity_maps_regions_grid_intensity_default import DEFAULT_GRID_INTENSITY_BY_ZONE
from ..utils.http_client import get_async_http_client
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
            logger.warning("ELECTRICITY_MAPS_TOKEN is not set in the environment. Using default values.")
            self.api_token = None
            self.headers = {}
        else:
            self.api_token = config.ELECTRICITY_MAPS_TOKEN
            self.headers = {"auth-token": self.api_token}

        # Reusable HTTP client (lazily initialized)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = get_async_http_client()
        return self._client

    async def close(self):
        """Close the reusable HTTP client to release connection pool resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def collect(self, zone: str, target_datetime: datetime = None) -> list:
        """
        Retrieves historical data for a specific zone and returns it.
        If the token is missing or the API call fails, it returns the default value.
        """
        if self.api_token:
            history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={zone}"
            logger.info(f"Fetching carbon intensity history for zone: {zone}...")

            client = await self._get_client()
            try:
                response = await client.get(history_url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                return data.get("history", [])
            except httpx.HTTPError as e:
                logger.error(f"Error fetching data from Electricity Maps API: {e}")
                # Fallback to default values below
            except Exception as e:
                logger.error(f"Unexpected error fetching data from Electricity Maps API: {e}")

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
