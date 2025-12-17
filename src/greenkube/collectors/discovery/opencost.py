# src/greenkube/collectors/discovery/opencost.py
import logging
from typing import Optional

import httpx

# Not needed for httpx exactly, but we handle verify=False
from greenkube.core.config import config
from greenkube.utils.http_client import get_async_http_client

from .base import BaseDiscovery

# Get logger for this module
logger = logging.getLogger(__name__)


class OpenCostDiscovery(BaseDiscovery):
    """
    Discovers a functional OpenCost endpoint by scoring candidates and
    probing the top contenders for a valid health check response.
    """

    async def discover(self) -> Optional[str]:
        candidates = await self._collect_candidates(
            "opencost",
            prefer_namespaces=("opencost",),
            prefer_ports=(9003, 8080),  # Port 9003 is common for OpenCost API
        )
        if not candidates:
            logger.info("OpenCost discovery: no candidates found after scoring.")
            return None

        result = await self.probe_candidates(candidates, self._probe_opencost_endpoint)

        if result:
            return result

        logger.warning("OpenCost discovery: Probed top candidates, but none responded to a /healthz check.")
        return None

    async def _probe_opencost_endpoint(self, base_url: str, score: int) -> bool:
        """
        Probes a candidate URL to see if it's a valid OpenCost endpoint.
        Checks /healthz for a 2xx response.
        """
        # Probe the /healthz endpoint instead of the base URL
        probe_url = f"{base_url.rstrip('/')}/healthz"

        verify_certs = config.OPENCOST_VERIFY_CERTS

        try:
            async with get_async_http_client(verify=verify_certs) as client:
                resp = await client.get(probe_url)
                status = resp.status_code

                logger.info(
                    "Probing OpenCost candidate %s (score=%s) at path /healthz -> status=%s",
                    base_url,
                    score,
                    status,
                )

                # OpenCost /healthz returns 200 OK on success
                if 200 <= status < 300:
                    return True

        except httpx.RequestError as e:
            logger.debug(
                "OpenCost probe failed for %s (score=%s) at path /healthz -> %s",
                base_url,
                score,
                e,
            )
        except Exception as e:
            logger.warning("Unexpected error probing OpenCost candidate %s: %s", base_url, e)

        return False
