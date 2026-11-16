import logging
from typing import Any, List, Optional

import httpx

from greenkube.collectors.base_collector import BaseCollector
from greenkube.core.config import config
from greenkube.models.boavizta import BoaviztaResponse

logger = logging.getLogger(__name__)

#: Providers of local or on-prem clusters, absent from Boavizta's cloud-instance catalogue.
NON_CLOUD_PROVIDERS = frozenset({"unknown", "local", "minikube", "on_prem", "on-prem", "kind", "k3s", "k3d"})


def is_cloud_provider(provider: Optional[str]) -> bool:
    """True when ``provider`` can be looked up in Boavizta's cloud-instance catalogue."""
    return bool(provider) and str(provider).strip().lower() not in NON_CLOUD_PROVIDERS


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
        self._client: httpx.AsyncClient | None = None
        #: Why the last lookup returned ``None`` (HTTP error, unexpected payload), for the negative cache.
        self.last_error: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self.headers,
                timeout=config.DEFAULT_TIMEOUT_CONNECT,
            )
        return self._client

    async def close(self):
        """Close the reusable HTTP client to release connection pool resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

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

        No ``duration`` is sent, so embodied impacts cover the whole life of the asset.
        Returns ``None`` (and sets :attr:`last_error`) when the API fails or answers an
        unexpected payload.
        """
        self.last_error = None

        # Local/on-prem clusters do not map to Boavizta's cloud-instance catalogue.
        if provider and instance_type:
            if not is_cloud_provider(provider):
                logger.debug(
                    "Skipping Boavizta cloud lookup for non-cloud provider %r; configured fallback will be used.",
                    provider,
                )
                if model:
                    return await self._get_server_archetype_impact(model, verbose, criteria)
                return None
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

        logger.info("Fetching Boavizta impact for cloud instance: %s %s", provider, instance_type)

        client = await self._get_client()
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return BoaviztaResponse.model_validate(response.json())
        except httpx.HTTPError as e:
            self.last_error = f"HTTP error: {e}"
            logger.error("Boavizta API error for cloud instance %s/%s: %s", provider, instance_type, e)
            return None
        except Exception as e:
            self.last_error = f"unexpected response: {e}"
            logger.error("Unexpected error calling Boavizta for %s/%s: %s", provider, instance_type, e)
            return None

    async def _get_server_archetype_impact(
        self, archetype: str, verbose: bool, criteria: str
    ) -> Optional[BoaviztaResponse]:
        url = f"{self.api_url}/v1/server/"
        params = {"archetype": archetype, "verbose": verbose, "criteria": criteria}

        logger.info("Fetching Boavizta impact for server archetype: %s", archetype)

        client = await self._get_client()
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return BoaviztaResponse.model_validate(response.json())
        except httpx.HTTPError as e:
            self.last_error = f"HTTP error: {e}"
            logger.error("Boavizta API error for archetype %s: %s", archetype, e)
            return None
        except Exception as e:
            self.last_error = f"unexpected response: {e}"
            logger.error("Unexpected error calling Boavizta for archetype %s: %s", archetype, e)
            return None
