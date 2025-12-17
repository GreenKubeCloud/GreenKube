# src/greenkube/collectors/discovery/prometheus.py
import logging
from typing import Optional

import httpx

# Suppress InsecureRequestWarning for self-signed certs in-cluster
# Not needed for httpx exactly, but we handle verify=False
from greenkube.core.config import config
from greenkube.utils.http_client import get_async_http_client

from .base import BaseDiscovery

# Get logger for this module
logger = logging.getLogger(__name__)


class PrometheusDiscovery(BaseDiscovery):
    """
    Discovers a functional Prometheus endpoint by scoring candidates and
    probing the top contenders for a valid Prometheus API response.
    """

    # A list of common API paths to probe for a 'query' endpoint.
    PROBE_PATHS = [
        "/api/v1/query",  # Standard Prometheus
        "/query",  # Some proxies
        "/prometheus/api/v1/query",  # Common when namespaced
    ]

    async def discover(self) -> Optional[str]:
        """
        Attempts to find a valid, running Prometheus service endpoint.
        """
        candidates = await self._collect_candidates(
            "prometheus",
            prefer_namespaces=("monitoring", "prometheus"),
            prefer_ports=(9090,),
            # Add labels to strongly prefer the 'kube-prometheus' stack default
            prefer_labels={
                "app.kubernetes.io/name": "prometheus",
                "app.kubernetes.io/instance": "k8s",
            },
        )
        if not candidates:
            logger.info("Prometheus discovery: no candidates found after scoring.")
            return None

        result = await self.probe_candidates(candidates, self._probe_prometheus_endpoint)

        if result:
            logger.info(f"Prometheus discovery: Successfully verified endpoint {result}")
            return result

        logger.warning("Prometheus discovery: Probed top candidates, but none responded with a valid Prometheus API.")
        return None

    async def _probe_prometheus_endpoint(self, base_url: str, score: int) -> bool:
        """
        Probes a candidate URL to see if it's a valid Prometheus query API.

        Returns True if a valid response is received, False otherwise.
        """
        for path in self.PROBE_PATHS:
            url = f"{base_url.rstrip('/')}{path}"
            verify_certs = config.PROMETHEUS_VERIFY_CERTS

            try:
                # Use shared async http client
                async with get_async_http_client(verify=verify_certs) as client:
                    # Use a simple 'up' query which is lightweight and universal
                    resp = await client.get(url, params={"query": "up"})
                    status = resp.status_code

                    try:
                        j = resp.json()
                        success = j.get("status") == "success"
                    except ValueError:
                        j = None
                        success = False

                    logger.info(
                        "Probing Prometheus candidate %s (score=%s) path=%s -> status=%s success=%s",
                        base_url,
                        score,
                        path,
                        status,
                        success,
                    )

                    # We require a 200 OK AND a json body with "status": "success"
                    if status == 200 and success:
                        return True

            except httpx.RequestError as e:
                logger.debug(
                    "Prometheus probe failed for %s (score=%s) path=%s -> %s",
                    base_url,
                    score,
                    path,
                    e,
                )
                # Continue to the next probe path
                continue
            except Exception as e:
                logger.warning("Unexpected error probing Prometheus candidate %s: %s", base_url, e)
                continue

        # If all probe paths fail for this base_url, return False
        return False
