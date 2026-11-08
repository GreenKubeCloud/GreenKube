import logging
from datetime import datetime, timezone

import httpx

from ..core.config import config
from ..core.observability import elapsed, record_collection_request, start_timer
from ..utils.http_client import get_async_http_client
from .base_electricity_provider import BaseElectricityProvider

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.electricitymaps.com/v3"


class ElectricityMapsCollector(BaseElectricityProvider):
    """
    A collector to retrieve carbon intensity data from the Electricity Maps API.
    It no longer handles saving to the database.
    """

    def __init__(self):
        """
        Initializes the collector. The 'zone' argument is no longer needed here.
        """
        if not config.ELECTRICITY_MAPS_TOKEN:
            logger.warning(
                "ELECTRICITY_MAPS_TOKEN is not set in the environment. Zone default intensities will be used."
            )
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

    async def collect(self, zone: str, target_datetime: datetime | None = None) -> list:  # type: ignore[override]
        """
        Retrieves historical data for a specific zone and returns it.

        Only measured values are returned. When the token is missing or the API
        call fails, an empty list is returned: static zone defaults are applied
        at read time by the calculator (tagged ``intensity_source=zone_default``)
        and never written into the intensity history (A8, BUG-07).
        """
        if not self.api_token:
            logger.info("No Electricity Maps token: zone %s will use its default intensity at read time.", zone)
            record_collection_request("electricity_maps", "skipped")
            return []

        history_url = f"{API_BASE_URL}/carbon-intensity/history?zone={zone}"
        if target_datetime:
            if target_datetime.tzinfo is None:
                target_datetime = target_datetime.replace(tzinfo=timezone.utc)
            history_url += f"&datetime={target_datetime.isoformat()}"
        logger.info("Fetching carbon intensity history for zone: %s...", zone)

        client = await self._get_client()
        timer = start_timer()
        try:
            response = await client.get(history_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            history = data.get("history", []) if isinstance(data, dict) else []
        except httpx.HTTPError as e:
            logger.error("Error fetching data from Electricity Maps API: %s", e)
            record_collection_request("electricity_maps", "error", elapsed(timer))
            return []
        except Exception as e:
            logger.error("Unexpected error fetching data from Electricity Maps API: %s", e)
            record_collection_request("electricity_maps", "error", elapsed(timer))
            return []
        record_collection_request("electricity_maps", "success" if history else "empty", elapsed(timer))
        return history
