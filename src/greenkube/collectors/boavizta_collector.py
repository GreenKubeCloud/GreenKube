import logging
from typing import Any, List, Optional

import httpx

from greenkube.collectors.base_collector import BaseCollector
from greenkube.core.config import config
from greenkube.models.boavizta import BoaviztaResponse

logger = logging.getLogger(__name__)


class BoaviztaCollector(BaseCollector):
    """
    Collector for retrieving embodied emissions data from Boavizta API.
    """

    def __init__(self):
        self.api_url = config.BOAVIZTA_API_URL
        self.api_token = config.BOAVIZTA_TOKEN
        self.headers = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    async def collect(self) -> List[Any]:
        """
        Not used for this collector as it is on-demand per node.
        """
        return []

    async def get_server_impact(
        self,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        instance_type: Optional[str] = None,
        verbose: bool = False,
        criteria: str = "gwp",
    ) -> Optional[BoaviztaResponse]:
        """
        Retrieves the impact of a server.
        Prioritizes cloud instance lookup if provider and instance_type are present.
        Falls back to model lookup if model is present.
        """

        # 1. Cloud Instance Lookup
        if provider and instance_type:
            return await self._get_cloud_instance_impact(provider, instance_type, verbose, criteria)

        # 2. Server Model Lookup (Archetype)
        if model:
            return await self._get_server_archetype_impact(model, verbose, criteria)

        logger.warning("Insufficient parameters for Boavizta lookup. Need (provider + instance_type) or (model).")
        return None

    async def _get_cloud_instance_impact(
        self, provider: str, instance_type: str, verbose: bool, criteria: str
    ) -> Optional[BoaviztaResponse]:
        url = f"{self.api_url}/v1/cloud/instance"
        params = {"provider": provider, "instance_type": instance_type, "verbose": verbose, "criteria": criteria}

        logger.info(f"Fetching Boavizta impact for cloud instance: {provider} {instance_type}")

        async with httpx.AsyncClient(headers=self.headers, timeout=config.DEFAULT_TIMEOUT_CONNECT) as client:
            try:
                response = await client.get(url, params=params)

                response.raise_for_status()
                return BoaviztaResponse(**response.json())
            except httpx.HTTPError as e:
                logger.error(f"Boavizta API error for cloud instance {provider}/{instance_type}: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error calling Boavizta for {provider}/{instance_type}: {e}")
                return None

    async def _get_server_archetype_impact(
        self, archetype: str, verbose: bool, criteria: str
    ) -> Optional[BoaviztaResponse]:
        url = f"{self.api_url}/v1/server/"
        params = {"archetype": archetype, "verbose": verbose, "criteria": criteria}

        logger.info(f"Fetching Boavizta impact for server archetype: {archetype}")

        async with httpx.AsyncClient(headers=self.headers, timeout=config.DEFAULT_TIMEOUT_CONNECT) as client:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return BoaviztaResponse(**response.json())
            except httpx.HTTPError as e:
                logger.error(f"Boavizta API error for archetype {archetype}: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error calling Boavizta for archetype {archetype}: {e}")
                return None
