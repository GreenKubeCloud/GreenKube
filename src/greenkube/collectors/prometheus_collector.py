# src/greenkube/collectors/prometheus_collector.py

"""
PrometheusCollector runs the range queries of the collection pipeline, with
endpoint discovery and authentication.
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.prometheus import PrometheusDiscovery
from greenkube.collectors.promql import PromQLCatalog
from greenkube.core.config import Config
from greenkube.utils.date_utils import ensure_utc, to_iso_z
from greenkube.utils.http_client import get_async_http_client

# Set up logging
logger = logging.getLogger(__name__)


class PrometheusQueryError(RuntimeError):
    """Prometheus could not answer a query (not configured, unreachable or error status)."""


class PrometheusCollector:
    """Runs Prometheus queries for the range collection pipeline."""

    def __init__(self, settings: Config):
        """
        Initializes the collector with settings and PromQL queries.
        """
        self.settings = settings
        self.base_url = settings.PROMETHEUS_URL
        self.query_range_step = settings.PROMETHEUS_QUERY_RANGE_STEP
        # Configured in http_client, but we can pass explicit timeouts if essential.
        # self.timeout = 10

        # TLS verify and auth support
        self.verify = getattr(settings, "PROMETHEUS_VERIFY_CERTS", True)
        self.bearer_token = getattr(settings, "PROMETHEUS_BEARER_TOKEN", None)
        self.username = getattr(settings, "PROMETHEUS_USERNAME", None)
        self.password = getattr(settings, "PROMETHEUS_PASSWORD", None)

        # Reusable HTTP client (lazily initialized)
        self._client: Optional[httpx.AsyncClient] = None

        # Every PromQL expression comes from the catalog (A1): container-scoped
        # series exclude the pod cgroup and pause container, see promql.py.
        self.promql = PromQLCatalog.from_config(settings)
        # Containers and pod cgroups in one query; callers choose per pod (COL-09).
        self.cpu_usage_query = self.promql.cpu_usage_by_level()
        # Use a BaseDiscovery instance to access in-cluster and DNS helpers
        # without duplicating logic.
        self._discovery = BaseDiscovery()

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the reusable HTTP client, creating it lazily if needed."""
        if self._client is None or self._client.is_closed:
            self._client = get_async_http_client(verify=self.verify)
        return self._client

    async def close(self):
        """Close the reusable HTTP client to release connection pool resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _query_prometheus(self, client: httpx.AsyncClient, query: str) -> List[Dict[str, Any]]:
        """
        Internal helper to run a query against the Prometheus API.

        Without a configured URL, discovery runs before the first request, as
        in ``collect_range``, so relative URLs are never queried (F-06).
        """
        if not self.base_url:
            await self._discover_and_update_url(client)
        if not self.base_url:
            logger.error("Prometheus URL is not configured and could not be discovered; query skipped: %s", query)
            return []
        base = self.base_url.rstrip("/")
        candidates = [
            f"{base}/api/v1/query",
            f"{base}/query",
            f"{base}/prometheus/api/v1/query",
        ]

        params = {"query": query}

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)

        return await self._query_run_loop(client, candidates, params, headers, auth, query)

    async def _query_run_loop(self, client, candidates, params, headers, auth, query):
        # Helper to avoid recursion depth or duplicated code
        last_err = None
        for query_url in candidates:
            try:
                response = await client.get(
                    query_url,
                    params=params,
                    headers=headers,
                    auth=auth,
                )
                response.raise_for_status()

                data = response.json()
                if data.get("status") != "success":
                    logger.warning(
                        "Prometheus returned non-success status for %s: %s",
                        query_url,
                        data.get("error", "Unknown"),
                    )
                    continue

                results = data.get("data", {}).get("result", [])
                logger.info("Prometheus at %s returned %d result(s)", query_url, len(results))
                return results

            except httpx.HTTPError as e:
                last_err = e
                logger.debug("Failed to connect to Prometheus at %s: %s", query_url, e)
                continue
            except Exception as e:
                last_err = e
                logger.error("Unexpected error querying Prometheus at %s: %s", query_url, e)
                continue

        if last_err:
            logger.error("All Prometheus query endpoints failed. Last error: %s", last_err)
        else:
            if not candidates:
                pass  # No URL set
            else:
                logger.error("Prometheus queries returned no results for query: %s", query)

        # Attempt discovery once when configured endpoints fail.
        # We need to act carefully. _discover_and_update_url might change self.base_url
        if await self._discover_and_update_url(client):
            return await self._query_prometheus(client, query)

        return []

    async def is_available(self) -> bool:
        client = await self._get_client()
        if self.base_url and await self._probe_url(client, self.base_url):
            return True

        if await self._discover_and_update_url(client):
            return True

        return False

    async def _probe_url(self, client: httpx.AsyncClient, u: str) -> bool:
        base = u.rstrip("/")
        candidates = [
            f"{base}/api/v1/query",
            f"{base}/query",
            f"{base}/prometheus/api/v1/query",
        ]

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)
        params = {"query": "up"}

        for url in candidates:
            try:
                resp = await client.get(url, params=params, headers=headers, auth=auth)
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success":
                    logger.debug("Prometheus is available at %s", url)
                    return True
            except Exception:
                continue
        return False

    def _update_url(self, url: str):
        self.base_url = url
        self.settings.PROMETHEUS_URL = url
        if url.startswith("https://"):
            self.verify = self.settings.PROMETHEUS_VERIFY_CERTS
        else:
            self.verify = False

    async def _discover_and_update_url(self, client: httpx.AsyncClient) -> bool:
        try:
            pd = PrometheusDiscovery()
            discovered = await pd.discover()
            if discovered and discovered != self.base_url:
                logger.info("Prometheus discovery returned %s", discovered)
                self._update_url(discovered)
                return True
        except Exception:
            logger.debug("Prometheus discovery attempt failed")

        if self._discovery._is_running_in_cluster():
            hosts = [
                "prometheus-k8s.monitoring.svc.cluster.local:9090",
                "prometheus-k8s.monitoring.svc.cluster.local:8080",
                "prometheus-operated.monitoring.svc.cluster.local:9090",
                "prometheus.monitoring.svc.cluster.local:9090",
            ]
            for host in hosts:
                for scheme in ("http", "https"):
                    url = f"{scheme}://{host}"
                    old_url = self.base_url
                    old_verify = self.verify
                    self._update_url(url)
                    if await self._probe_url(client, url):
                        logger.info("Prometheus responded at well-known URL %s", url)
                        return True
                    self.base_url = old_url
                    self.verify = old_verify
        return False

    async def collect_range(
        self,
        start,
        end,
        step: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Run a range query; evaluation points are ``start, start + step, ..., end``.

        An empty list means Prometheus answered with no series.  When
        Prometheus is not configured, unreachable or rejects the query,
        :class:`PrometheusQueryError` is raised so that callers never record
        an outage as an empty collection window (A2).
        """
        step = step or self.query_range_step
        q = query or self.cpu_usage_query
        try:
            params: Dict[str, Any] = {
                "query": q,
                "start": to_iso_z(ensure_utc(start)),
                "end": to_iso_z(ensure_utc(end)),
                "step": step,
            }
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"Invalid range query bounds: {start!r} -> {end!r}") from error

        client = await self._get_client()
        if not self.base_url:
            await self._discover_and_update_url(client)
        if not self.base_url:
            raise PrometheusQueryError("Prometheus URL is not configured and could not be discovered.")

        base = self.base_url.rstrip("/")
        candidates = [
            f"{base}/api/v1/query_range",
            f"{base}/query_range",
            f"{base}/prometheus/api/v1/query_range",
        ]

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)

        last_error: str = "no endpoint answered"
        for query_url in candidates:
            try:
                response = await client.get(query_url, params=params, headers=headers, auth=auth)
                response.raise_for_status()
                data = response.json()
            except Exception as error:
                last_error = f"{query_url}: {error}"
                continue
            if data.get("status") != "success":
                last_error = f"{query_url}: {data.get('error') or data.get('status')}"
                continue

            # No empty-result fallback: the default query reads containers and pod cgroups together (COL-09).
            return data.get("data", {}).get("result", [])
        raise PrometheusQueryError(f"Prometheus range query failed ({last_error}).")
