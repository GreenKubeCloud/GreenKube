import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

# from requests.packages.urllib3.exceptions import InsecureRequestWarning
# Removing InsecureRequestWarning import if not used by httpx directly or handle differently.
# Httpx doesn't emit InsecureRequestWarning in same way usually, just ignore verify=False.
from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.opencost import OpenCostDiscovery

from ..core.config import config
from ..models.metrics import CostMetric
from ..utils.http_client import get_async_http_client
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service via an Ingress.
    The endpoint URL is read from application configuration (`config.OPENCOST_API_URL`).
    """

    def __init__(self):
        # session is no longer maintained as persistent member
        pass

    async def collect(self, window: str = "1d", timestamp: Optional[datetime] = None) -> List[CostMetric]:
        """
        Fetches cost data from OpenCost by making an HTTP request to its API.

        Returns:
            A list of CostMetric objects, or an empty list if an error occurs.
        """
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        params = {"window": window, "aggregate": "pod"}
        verify_certs = config.OPENCOST_VERIFY_CERTS

        async with get_async_http_client(verify=verify_certs) as client:
            # Resolve URL (Config -> Discovery -> Local -> In-Cluster)
            url = await self._resolve_url(client)

            if not url:
                logger.error("OpenCost API URL is not configured and discovery failed; skipping OpenCost collection")
                return []

            async def _fetch(u: str):
                try:
                    resp = await client.get(u, params=params)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.debug("Request to %s failed: %s", u, exc)
                    return None

                try:
                    return resp.json().get("data")
                except Exception:
                    # httpx doesn't separate json decode easily, usually ValueError? No, json.JSONDecodeError
                    logger.error("Failed to decode JSON from %s. Server sent non-JSON response.", u)
                    logger.debug("Raw response content from %s: %s", u, resp.text[:500])
                    return None

            response_data = await _fetch(url)

            # If the base url returned no usable data, try appending the well-known
            # allocation path used by some OpenCost deployments.
            if not response_data:
                alt_path = url.rstrip("/") + "/allocation/compute"
                logger.debug("Trying alternative OpenCost path: %s", alt_path)
                response_data = await _fetch(alt_path)
                if response_data:
                    # Prefer the alt path for future calls
                    setattr(config, "OPENCOST_API_URL", alt_path)

            if not response_data or not isinstance(response_data, list) or len(response_data) == 0:
                logger.warning(
                    "OpenCost API returned no data. This can happen if the cluster is new or the endpoint is different."
                )
                return []

            cost_data = response_data[0]

        collected_metrics = []
        now = timestamp if timestamp else datetime.now(timezone.utc)

        for resource_id, item in cost_data.items():
            properties = item.get("properties", {})
            pod_name = properties.get("pod")
            namespace = properties.get("namespace")

            if not pod_name:
                pod_name = resource_id

            if not namespace:
                logger.warning("Skipping metric for '%s' because namespace is missing.", pod_name)
                continue

            metric = CostMetric(
                pod_name=pod_name,
                namespace=namespace,
                cpu_cost=item.get("cpuCost", 0.0),
                ram_cost=item.get("ramCost", 0.0),
                total_cost=item.get("totalCost", 0.0),
                timestamp=now,
            )
            collected_metrics.append(metric)

        logger.info("Successfully collected %d metrics from OpenCost.", len(collected_metrics))
        return collected_metrics

    async def collect_range(self, start, end) -> List[CostMetric]:
        """Collect cost allocation data for a time range."""
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        window = f"{start_ts},{end_ts}"
        return await self.collect(window=window, timestamp=end)

    async def is_available(self) -> bool:
        """
        Quick probe to check if the OpenCost API URL is reachable and returns a
        2xx response. Returns True when reachable, False otherwise.
        """
        verify_certs = config.OPENCOST_VERIFY_CERTS
        async with get_async_http_client(verify=verify_certs) as client:
            url = getattr(config, "OPENCOST_API_URL", None)
            # If not configured, try discovery first
            if not url:
                try:
                    od = OpenCostDiscovery()
                    discovered = await od.discover()
                    if discovered:
                        setattr(config, "OPENCOST_API_URL", discovered)
                        url = discovered
                except Exception:
                    logger.debug("OpenCost discovery attempt failed or returned no candidate")

            if url and await self._probe(client, url):
                logger.debug("OpenCost API is available at %s", url)
                return True

            # If configured URL didn't respond, try the well-known allocation path
            if url:
                alt = url.rstrip("/") + "/allocation/compute"
                if await self._probe(client, alt):
                    logger.debug("OpenCost API is available at alternative path %s", alt)
                    setattr(config, "OPENCOST_API_URL", alt)
                    return True

            # try discovery as a fallback
            try:
                od = OpenCostDiscovery()
                discovered = await od.discover()
                if discovered and await self._probe(client, discovered):
                    # update the config so subsequent calls use discovered URL
                    setattr(config, "OPENCOST_API_URL", discovered)
                    return True
            except Exception:
                logger.debug("OpenCost discovery/probe failed")

            logger.debug("OpenCost API is not available")
            return False

    async def _resolve_url(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Resolves the OpenCost API URL by checking config, discovery, and fallback candidates.
        """
        # 1. Configured URL
        url = getattr(config, "OPENCOST_API_URL", None)
        if url:
            return url

        # 2. Discovery
        try:
            od = OpenCostDiscovery()
            discovered = await od.discover()
            if discovered:
                setattr(config, "OPENCOST_API_URL", discovered)
                return discovered
        except Exception:
            logger.debug("OpenCost discovery failed")

        # 3. Localhost candidates
        candidates = [
            "http://localhost:9003",
            "http://opencost:9003",
            "http://localhost:9090",
        ]
        for candidate in candidates:
            if await self._probe(client, candidate):
                setattr(config, "OPENCOST_API_URL", candidate)
                return candidate

        # 4. In-cluster candidates
        bd = BaseDiscovery()
        if bd._is_running_in_cluster():
            cluster_candidates = [
                "http://opencost.opencost.svc.cluster.local:9003",
                "http://opencost.svc.cluster.local:9003",
                "http://opencost.opencost.svc.cluster.local:9090",
            ]
            for candidate in cluster_candidates:
                if await self._probe(client, candidate):
                    setattr(config, "OPENCOST_API_URL", candidate)
                    return candidate

        return None

    async def _probe(self, client: httpx.AsyncClient, url: str) -> bool:
        """
        Probes a URL to see if it's a valid OpenCost endpoint.
        """
        try:
            params = {"window": "1d", "aggregate": "pod"}
            r = await client.get(url, params=params)
            r.raise_for_status()
            return True
        except Exception:
            return False
