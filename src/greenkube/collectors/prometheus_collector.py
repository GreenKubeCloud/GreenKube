# src/greenkube/collectors/prometheus_collector.py

"""
PrometheusCollector fetches CPU usage and node instance type metrics.
This data is the input for the BasicEstimator.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from pydantic import ValidationError

from greenkube.collectors.base_collector import BaseCollector
from greenkube.collectors.discovery.base import BaseDiscovery
from greenkube.collectors.discovery.prometheus import PrometheusDiscovery
from greenkube.core.config import Config
from greenkube.models.prometheus_metrics import (
    NodeInstanceType,
    PodCPUUsage,
    PrometheusMetric,
)
from greenkube.utils.date_utils import ensure_utc, to_iso_z
from greenkube.utils.http_client import get_async_http_client

# Set up logging
logger = logging.getLogger(__name__)


class PrometheusCollector(BaseCollector):
    """
    Collects essential metrics from Prometheus for the estimation engine.
    """

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

        # Query for average CPU usage in cores over the last step.
        # We filter for containers with a name, which is standard.
        self.cpu_usage_query = (
            f"sum(rate(container_cpu_usage_seconds_total{{container!=''}}[{self.query_range_step}])) "
            "by (namespace, pod, container, node)"
        )

        # Prometheus label key used to map node -> instance type may vary across setups.
        self.node_instance_label_key = getattr(
            settings,
            "PROMETHEUS_NODE_INSTANCE_LABEL",
            "label_node_kubernetes_io_instance_type",
        )
        # Build a query that filters nodes which have the configured instance-type label.
        self.node_labels_query = f"kube_node_labels{{{self.node_instance_label_key}!=''}}"
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

    async def collect(self) -> PrometheusMetric:
        """
        Fetch all required metrics from Prometheus.

        Returns a PrometheusMetric object containing lists of parsed data.
        """
        client = await self._get_client()
        # Launch queries concurrently to reduce latency
        cpu_future = self._query_prometheus(client, self.cpu_usage_query)
        node_future = self._query_prometheus(client, self.node_labels_query)

        # Wait for both primary queries
        cpu_results, node_results = await asyncio.gather(cpu_future, node_future)

        # If the standard grouped query returned no results, try fallback
        if not cpu_results:
            fallback_query = (
                f"sum(rate(container_cpu_usage_seconds_total[{self.query_range_step}])) by (namespace, pod, node)"
            )
            logger.info("Falling back to container_cpu_usage grouped without 'container' label")
            cpu_results = await self._query_prometheus(client, fallback_query)

        parsed_cpu_usage = []
        malformed_cpu_count = 0
        malformed_cpu_examples = []
        non_pod_skipped = 0

        for item in cpu_results:
            metric = item.get("metric", {})
            if not metric or "namespace" not in metric or "pod" not in metric:
                logger.debug("Skipping non-pod Prometheus series: %s", item)
                non_pod_skipped += 1
                continue

            parsed_item = self._parse_cpu_data(item)
            if not parsed_item:
                parsed_item = self._parse_cpu_data_no_container(item)

            if parsed_item:
                parsed_cpu_usage.append(parsed_item)
            else:
                malformed_cpu_count += 1
                if len(malformed_cpu_examples) < 3:
                    malformed_cpu_examples.append(item)

        parsed_node_types = []
        malformed_node_count = 0
        malformed_node_examples = []
        for item in node_results:
            parsed_item = self._parse_node_data(item)
            if parsed_item:
                parsed_node_types.append(parsed_item)
            else:
                malformed_node_count += 1
                if len(malformed_node_examples) < 3:
                    malformed_node_examples.append(item)

        if not parsed_node_types:
            logger.info(
                "No node instance-type labels found for label '%s'; using DEFAULT_INSTANCE_PROFILE",
                self.node_instance_label_key,
            )

        if malformed_cpu_count:
            logger.warning(
                "Skipped %d malformed CPU metric item(s). Examples: %s",
                malformed_cpu_count,
                malformed_cpu_examples,
            )

        if non_pod_skipped:
            logger.info(
                "Skipped %d non-pod Prometheus series during CPU query (these are node-level or aggregate series).",
                non_pod_skipped,
            )

        if malformed_node_count:
            logger.warning(
                "Skipped %d malformed node metric item(s). Examples: %s",
                malformed_node_count,
                malformed_node_examples,
            )

        return PrometheusMetric(
            pod_cpu_usage=parsed_cpu_usage,
            node_instance_types=parsed_node_types,
        )

    async def _query_prometheus(self, client: httpx.AsyncClient, query: str) -> List[Dict[str, Any]]:
        """
        Internal helper to run a query against the Prometheus API.
        """
        base = self.base_url.rstrip("/") if self.base_url else ""
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

    # Parsing methods _parse_cpu_data ... remain unchanged/same logic

    async def collect_range(
        self, start, end, step: Optional[str] = None, query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        step = step or self.query_range_step
        q = query or self.cpu_usage_query

        client = await self._get_client()
        if not self.base_url:
            # Attempt discovery
            await self._discover_and_update_url(client)

        if not self.base_url:
            # log warning
            return []

        base = self.base_url.rstrip("/")
        candidates = [
            f"{base}/api/v1/query_range",
            f"{base}/query_range",
            f"{base}/prometheus/api/v1/query_range",
        ]
        params = {"query": q, "start": None, "end": None, "step": step}
        try:
            params["start"] = to_iso_z(ensure_utc(start))
            params["end"] = to_iso_z(ensure_utc(end))
        except ValueError:
            return []

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)

        for query_url in candidates:
            try:
                response = await client.get(query_url, params=params, headers=headers, auth=auth)
                response.raise_for_status()
                data = response.json()
                # Check success
                if data.get("status") != "success":
                    continue

                results = data.get("data", {}).get("result", [])
                if not results and "container" in q:
                    # Fallback
                    fallback_query = (
                        f"sum(rate(container_cpu_usage_seconds_total[{self.query_range_step}]))"
                        " by (namespace, pod, node)"
                    )
                    return await self.collect_range(start, end, step=step, query=fallback_query)

                return results

            except Exception:
                continue
        return []

    # Keeping parsing methods
    def _parse_cpu_data(self, item: Dict[str, Any]) -> Optional[PodCPUUsage]:
        metric = item.get("metric", {})
        value_str = item.get("value", [None, None])[1]
        if value_str is None or value_str == "NaN":
            return None
        namespace = metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("k8s_namespace")
        pod = metric.get("pod") or metric.get("pod_name") or metric.get("kubernetes_pod_name")
        container = metric.get("container") or metric.get("container_name")
        node = metric.get("node") or metric.get("kubernetes_node")
        if not all((namespace, pod, container, node)):
            return None
        try:
            return PodCPUUsage(
                namespace=namespace, pod=pod, container=container, node=node, cpu_usage_cores=float(value_str)
            )
        except (TypeError, ValueError, ValidationError):
            # Validation failed (bad types); return None for aggregation by caller.
            return None

    def _parse_cpu_data_no_container(self, item: Dict[str, Any]) -> Optional[PodCPUUsage]:
        """
        Parses CPU metric items that don't include a 'container' label.
        Maps container to an empty string and attempts to extract namespace/pod/node.
        """
        metric = item.get("metric", {})
        value_str = item.get("value", [None, None])[1]

        if value_str is None or value_str == "NaN":
            return None

        # namespace, pod and node are required; container may be absent and will be
        # set to empty string.
        namespace = metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("k8s_namespace")
        pod = metric.get("pod") or metric.get("pod_name") or metric.get("kubernetes_pod_name")
        node = metric.get("node") or metric.get("kubernetes_node")
        container = metric.get("container") or metric.get("container_name") or ""

        if not all((namespace, pod, node)):
            return None

        try:
            return PodCPUUsage(
                namespace=namespace, pod=pod, container=container, node=node, cpu_usage_cores=float(value_str)
            )
        except (TypeError, ValueError, ValidationError):
            return None

    def _parse_node_data(self, item: Dict[str, Any]) -> Optional[NodeInstanceType]:
        metric = item.get("metric", {})
        label_key = self.node_instance_label_key
        if not all(k in metric for k in ("node", label_key)):
            return None
        try:
            return NodeInstanceType(node=metric["node"], instance_type=metric[label_key])
        except ValidationError:
            return None
