# src/greenkube/collectors/discovery/opencost.py
import logging
import warnings
from typing import Optional

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from greenkube.core.config import config

from .base import BaseDiscovery

# Get logger for this module
logger = logging.getLogger(__name__)


class OpenCostDiscovery(BaseDiscovery):
    """
    Discovers a functional OpenCost endpoint by scoring candidates and
    probing the top contenders for a valid health check response.
    """

    def discover(self) -> Optional[str]:
        candidates = self._collect_candidates(
            "opencost",
            prefer_namespaces=("opencost",),
            prefer_ports=(9003, 8080),  # Port 9003 is common for OpenCost API
        )
        if not candidates:
            logger.info("OpenCost discovery: no candidates found after scoring.")
            return None

        result = self.probe_candidates(candidates, self._probe_opencost_endpoint)

        if result:
            return result

        logger.warning("OpenCost discovery: Probed top candidates, but none responded to a /healthz check.")
        return None

    def _probe_opencost_endpoint(self, base_url: str, score: int) -> bool:
        """
        Probes a candidate URL to see if it's a valid OpenCost endpoint.
        Checks /healthz for a 2xx response.
        """
        # Probe the /healthz endpoint instead of the base URL
        probe_url = f"{base_url.rstrip('/')}/healthz"

        verify_certs = config.OPENCOST_VERIFY_CERTS
        # Only suppress SSL warnings if verification is explicitly disabled
        if not verify_certs:
            warnings.simplefilter("ignore", InsecureRequestWarning)

        try:
            resp = requests.get(probe_url, timeout=3, verify=verify_certs)
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

        except requests.exceptions.RequestException as e:
            logger.debug(
                "OpenCost probe failed for %s (score=%s) at path /healthz -> %s",
                base_url,
                score,
                e,
            )

        return False
