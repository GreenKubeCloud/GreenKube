# src/greenkube/collectors/opencost_collector.py
"""
This module contains the collector responsible for gathering cost
allocation data from the OpenCost API.
"""

import logging
import warnings
from datetime import datetime, timezone
from typing import List, Optional

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.opencost import OpenCostDiscovery

from ..core.config import config
from ..models.metrics import CostMetric
from ..utils.http_client import get_http_session
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service via an Ingress.
    The endpoint URL is read from application configuration (`config.OPENCOST_API_URL`).
    """

    def __init__(self):
        self.session = get_http_session()

    def collect(self, window: str = "1d", timestamp: Optional[datetime] = None) -> List[CostMetric]:
        """
        Fetches cost data from OpenCost by making an HTTP request to its API.

        Returns:
            A list of CostMetric objects, or an empty list if an error occurs.
        """
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        params = {"window": window, "aggregate": "pod"}
        verify_certs = config.OPENCOST_VERIFY_CERTS

        try:
            # Only suppress SSL warnings if verification is explicitly disabled
            if not verify_certs:
                warnings.simplefilter("ignore", InsecureRequestWarning)

            # Resolve URL (Config -> Discovery -> Local -> In-Cluster)
            url = self._resolve_url()

            if not url:
                logger.error("OpenCost API URL is not configured and discovery failed; skipping OpenCost collection")
                return []

            def _fetch(u: str):
                try:
                    # Use robust session
                    resp = self.session.get(u, params=params, verify=verify_certs)
                    resp.raise_for_status()
                except requests.exceptions.RequestException as exc:
                    logger.debug("Request to %s failed: %s", u, exc)
                    return None

                try:
                    return resp.json().get("data")
                except requests.exceptions.JSONDecodeError:
                    logger.error("Failed to decode JSON from %s. Server sent non-JSON response.", u)
                    logger.debug("Raw response content from %s: %s", u, resp.text[:500])
                    return None

            response_data = _fetch(url)

            # If the base url returned no usable data, try appending the well-known
            # allocation path used by some OpenCost deployments.
            if not response_data:
                alt_path = url.rstrip("/") + "/allocation/compute"
                logger.debug("Trying alternative OpenCost path: %s", alt_path)
                response_data = _fetch(alt_path)
                if response_data:
                    # Prefer the alt path for future calls
                    setattr(config, "OPENCOST_API_URL", alt_path)

            if not response_data or not isinstance(response_data, list) or len(response_data) == 0:
                logger.warning(
                    "OpenCost API returned no data. This can happen if the cluster is new or the endpoint is different."
                )
                return []

            cost_data = response_data[0]

        except requests.exceptions.RequestException as e:
            logger.error("Could not connect to OpenCost API via Ingress: %s", e)
            return []

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

    def collect_range(self, start, end) -> List[CostMetric]:
        """Collect cost allocation data for a time range.

        The OpenCost Ingress API used here does not always provide a direct
        range endpoint; this method calls the same ingress but allows the
        caller to provide start/end in case the backend supports it later.
        Currently it proxies to `collect()` for compatibility.
        """
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        window = f"{start_ts},{end_ts}"
        return self.collect(window=window, timestamp=end)

    def is_available(self) -> bool:
        """
        Quick probe to check if the OpenCost API URL is reachable and returns a
        2xx response. Returns True when reachable, False otherwise.
        """
        url = getattr(config, "OPENCOST_API_URL", None)
        # If not configured, try discovery first
        if not url:
            try:
                od = OpenCostDiscovery()
                discovered = od.discover()
                if discovered:
                    setattr(config, "OPENCOST_API_URL", discovered)
                    url = discovered
            except Exception:
                logger.debug("OpenCost discovery attempt failed or returned no candidate")

        if url and self._probe(url):
            logger.debug("OpenCost API is available at %s", url)
            return True

        # If configured URL didn't respond, try the well-known allocation path
        if url:
            alt = url.rstrip("/") + "/allocation/compute"
            if self._probe(alt):
                logger.debug("OpenCost API is available at alternative path %s", alt)
                setattr(config, "OPENCOST_API_URL", alt)
                return True

        # try discovery as a fallback
        try:
            od = OpenCostDiscovery()
            discovered = od.discover()
            if discovered and self._probe(discovered):
                # update the config so subsequent calls use discovered URL
                setattr(config, "OPENCOST_API_URL", discovered)
                return True
        except Exception:
            logger.debug("OpenCost discovery/probe failed")

        logger.debug("OpenCost API is not available")
        return False

    def _resolve_url(self) -> Optional[str]:
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
            discovered = od.discover()
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
            if self._probe(candidate):
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
                if self._probe(candidate):
                    setattr(config, "OPENCOST_API_URL", candidate)
                    return candidate

        return None

    def _probe(self, url: str) -> bool:
        """
        Probes a URL to see if it's a valid OpenCost endpoint.
        """
        verify_certs = config.OPENCOST_VERIFY_CERTS
        try:
            params = {"window": "1d", "aggregate": "pod"}
            # Use robust session (timeout handled by session adapter)
            r = self.session.get(url, params=params, verify=verify_certs)
            r.raise_for_status()
            return True
        except Exception:
            return False
