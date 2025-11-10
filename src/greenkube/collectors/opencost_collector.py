# src/greenkube/collectors/opencost_collector.py
"""
This module contains the collector responsible for gathering cost
allocation data from the OpenCost API.
"""

import logging
from datetime import datetime, timezone
from typing import List

import requests

from greenkube.collectors.discovery.opencost import OpenCostDiscovery

from ..core.config import config
from ..models.metrics import CostMetric
from .base_collector import BaseCollector

logger = logging.getLogger(__name__)


class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service via an Ingress.
    The endpoint URL is read from application configuration (`config.OPENCOST_API_URL`).
    """

    def collect(self) -> List[CostMetric]:
        """
        Fetches cost data from OpenCost by making an HTTP request to its API.

        Returns:
            A list of CostMetric objects, or an empty list if an error occurs.
        """
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        params = {"window": "1d", "aggregate": "pod"}

        try:
            import warnings

            from requests.packages.urllib3.exceptions import InsecureRequestWarning

            warnings.simplefilter("ignore", InsecureRequestWarning)

            # Use the configured OpenCost API URL (can be overridden via env var OPENCOST_API_URL)
            url = getattr(config, "OPENCOST_API_URL", None)
            if not url:
                try:
                    od = OpenCostDiscovery()
                    discovered = od.discover()
                    if discovered:
                        url = discovered
                        setattr(config, "OPENCOST_API_URL", discovered)
                except Exception:
                    logger.debug("OpenCost discovery failed during collect; will attempt with current config value")

            if not url:
                # Try a small set of common local candidates before giving up.
                # This helps unit tests that mock requests.get but don't set
                # config.OPENCOST_API_URL, and avoids hard failure when discovery
                # can't reach the cluster API during CI/deploy.
                for candidate in (
                    "http://localhost:9003",
                    "http://opencost:9003",
                    "http://localhost:9090",
                ):
                    try:
                        r = requests.get(candidate, params=params, timeout=5, verify=False)
                        r.raise_for_status()
                        url = candidate
                        setattr(config, "OPENCOST_API_URL", url)
                        break
                    except Exception:
                        continue

                if not url:
                    logger.error(
                        "OpenCost API URL is not configured and discovery failed; skipping OpenCost collection"
                    )
                    return []

            # If still not configured and we're running in-cluster, try well-known
            # service DNS names used by typical OpenCost Helm charts.
            from greenkube.collectors.discovery.base import BaseDiscovery

            bd = BaseDiscovery()
            if not url and bd._is_running_in_cluster():
                for candidate in (
                    "http://opencost.opencost.svc.cluster.local:9003",
                    "http://opencost.svc.cluster.local:9003",
                    "http://opencost.opencost.svc.cluster.local:9090",
                ):
                    try:
                        r = requests.get(candidate, timeout=5)
                        r.raise_for_status()
                        url = candidate
                        setattr(config, "OPENCOST_API_URL", url)
                        break
                    except Exception:
                        continue

            def _fetch(u: str):
                try:
                    resp = requests.get(u, params=params, timeout=10, verify=False)
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
        now = datetime.now(timezone.utc)

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
        # If the OpenCost API supported a range, we'd pass start/end params.
        # For now, keep behavior identical to collect(), returning latest window.
        return self.collect()

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

        def _probe(u: str) -> bool:
            try:
                resp = requests.get(u, timeout=5, verify=False)
                return 200 <= resp.status_code < 300
            except Exception:
                return False

        if url and _probe(url):
            logger.debug("OpenCost API is available at %s", url)
            return True

        # If configured URL didn't respond, try the well-known allocation path
        if url:
            alt = url.rstrip("/") + "/allocation/compute"
            if _probe(alt):
                logger.debug("OpenCost API is available at alternative path %s", alt)
                setattr(config, "OPENCOST_API_URL", alt)
                return True

        # try discovery as a fallback
        try:
            od = OpenCostDiscovery()
            discovered = od.discover()
            if discovered and _probe(discovered):
                # update the config so subsequent calls use discovered URL
                setattr(config, "OPENCOST_API_URL", discovered)
                return True
        except Exception:
            logger.debug("OpenCost discovery/probe failed")

        logger.debug("OpenCost API is not available")
        return False
