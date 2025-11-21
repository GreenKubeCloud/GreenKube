# src/greenkube/collectors/prometheus_collector.py

"""
PrometheusCollector fetches CPU usage and node instance type metrics.
This data is the input for the BasicEstimator.
"""

import logging
import os
from datetime import timezone
from typing import Any, Dict, List, Optional

import requests
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
        self.timeout = 10

        # TLS verify and auth support
        self.verify = getattr(settings, "PROMETHEUS_VERIFY_CERTS", True)
        self.bearer_token = getattr(settings, "PROMETHEUS_BEARER_TOKEN", None)
        self.username = getattr(settings, "PROMETHEUS_USERNAME", None)
        self.password = getattr(settings, "PROMETHEUS_PASSWORD", None)

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
        # without duplicating logic. This keeps tests from needing a full
        # PrometheusDiscovery instance and prevents AttributeError when
        # calling _is_running_in_cluster() in probe fallbacks.
        self._discovery = BaseDiscovery()

    def collect(self) -> PrometheusMetric:
        """
        Fetch all required metrics from Prometheus.

        Returns a PrometheusMetric object containing lists of parsed data.
        """
        cpu_results = self._query_prometheus(self.cpu_usage_query)
        # If the standard grouped query returned no results (some setups omit the 'container' label),
        # try a fallback that doesn't require 'container' and groups only by namespace/pod/node.
        if not cpu_results:
            fallback_query = (
                f"sum(rate(container_cpu_usage_seconds_total[{self.query_range_step}])) by (namespace, pod, node)"
            )
            logger.info("Falling back to container_cpu_usage grouped without 'container' label")
            cpu_results = self._query_prometheus(fallback_query)
        node_results = self._query_prometheus(self.node_labels_query)

        parsed_cpu_usage = []
        malformed_cpu_count = 0
        malformed_cpu_examples = []
        non_pod_skipped = 0

        for item in cpu_results:
            # Many Prometheus setups return node-level aggregates or other series that do
            # not include pod/namespace labels. We only care about pod-level metrics here.
            metric = item.get("metric", {})
            if not metric or "namespace" not in metric or "pod" not in metric:
                # Skip non-pod series; emit a DEBUG-level message so developers can
                # inspect skipped series without polluting INFO/WARN logs.
                logger.debug("Skipping non-pod Prometheus series: %s", item)
                non_pod_skipped += 1
                continue

            # Try parsing with container first; if labels are missing, try the no-container parser.
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

    def _query_prometheus(self, query: str) -> List[Dict[str, Any]]:
        """
        Internal helper to run a query against the Prometheus API.

        Returns the 'result' list from the JSON response, or [] on failure.
        """
        # If base_url is not set, this part will fail and fall through to discovery.
        # Normalize base URL and try a couple of common endpoint forms.
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

        last_err = None
        for query_url in candidates:
            try:
                logger.info("Querying Prometheus at %s", query_url)
                response = requests.get(
                    query_url,
                    params=params,
                    headers=headers,
                    auth=auth,
                    timeout=self.timeout,
                    verify=self.verify,
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

            except requests.exceptions.SSLError as e:
                last_err = e
                logger.warning("TLS/SSL error querying Prometheus at %s: %s", query_url, e)
                continue
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.debug("Failed to connect to Prometheus at %s: %s", query_url, e)
                continue
            except Exception as e:
                last_err = e
                logger.error("Unexpected error querying Prometheus at %s: %s", query_url, e)
                continue

        # If we reach here, all candidates failed
        if last_err:
            logger.error("All Prometheus query endpoints failed. Last error: %s", last_err)
        else:
            logger.error("Prometheus queries returned no results for query: %s", query)

        # Attempt discovery once when configured endpoints fail.
        if self._discover_and_update_url():
            # Retry with new URL
            return self._query_prometheus(query)

        return []

    def is_available(self) -> bool:
        """
        Probe Prometheus endpoints quickly to determine availability.

        Returns True if at least one candidate query endpoint responds with
        a success-like payload (HTTP 200 and JSON with status 'success').
        If no configured base URL is reachable, attempt cluster service
        discovery and probe the discovered DNS endpoint.
        """

        # If configured URL works, we're good
        if self.base_url and self._probe_url(self.base_url):
            return True

        # Try discovery and probe the discovered endpoint
        if self._discover_and_update_url():
            return True

        logger.debug("Prometheus is not available on any candidate endpoints")
        return False

    def _probe_url(self, u: str) -> bool:
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
                # When probing, we might want to be lenient with verify if it's a new URL
                # but for now use self.verify which might have been updated by _update_url
                resp = requests.get(url, params=params, headers=headers, auth=auth, timeout=3, verify=self.verify)
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success":
                    logger.debug("Prometheus is available at %s", url)
                    return True
            except Exception:
                logger.debug("Prometheus probe failed for %s", url)
                continue
        return False

    def _update_url(self, url: str):
        self.base_url = url
        self.settings.PROMETHEUS_URL = url
        if url.startswith("https://"):
            env_val = os.getenv("PROMETHEUS_VERIFY_CERTS")
            if env_val is not None:
                self.verify = env_val.lower() in ("true", "1", "t", "y", "yes")
            else:
                self.verify = False
        else:
            # Reset verify to default or keep as is?
            # If switching from https to http, verify doesn't matter much.
            pass

    def _discover_and_update_url(self) -> bool:
        try:
            pd = PrometheusDiscovery()
            discovered = pd.discover()
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
                    # Temporarily update URL to probe it with correct verify settings
                    old_url = self.base_url
                    old_verify = self.verify
                    self._update_url(url)
                    if self._probe_url(url):
                        logger.info("Prometheus responded at well-known URL %s", url)
                        return True
                    # Revert if failed
                    self.base_url = old_url
                    self.verify = old_verify
        return False

    def _parse_cpu_data(self, item: Dict[str, Any]) -> Optional[PodCPUUsage]:
        """
        Parses a single item from the CPU query result.
        Returns a PodCPUUsage model or None if parsing fails.
        """
        metric = item.get("metric", {})
        # Value is an array like [<timestamp>, "<value>"]. We take the string value.
        value_str = item.get("value", [None, None])[1]

        # If the metric contains NaN or missing value, treat as unparsable and return None.
        if value_str is None or value_str == "NaN":
            return None

        # Validate required labels are present; if any are missing, return None and let
        # the caller aggregate malformed items for a single warning.
        namespace = metric.get("namespace") or metric.get("kubernetes_namespace") or metric.get("k8s_namespace")
        pod = metric.get("pod") or metric.get("pod_name") or metric.get("kubernetes_pod_name")
        container = metric.get("container") or metric.get("container_name")
        node = metric.get("node") or metric.get("kubernetes_node")

        if not all((namespace, pod, container, node)):
            return None

        try:
            data_to_validate = {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "node": node,
                "cpu_usage_cores": float(value_str),
            }
            return PodCPUUsage(**data_to_validate)
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
            data_to_validate = {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "node": node,
                "cpu_usage_cores": float(value_str),
            }
            return PodCPUUsage(**data_to_validate)
        except (TypeError, ValueError, ValidationError):
            return None

    def _parse_node_data(self, item: Dict[str, Any]) -> Optional[NodeInstanceType]:
        """
        Parses a single item from the node labels query result.
        Returns a NodeInstanceType model or None if parsing fails.
        """
        metric = item.get("metric", {})

        # Use the configured instance label key (it may vary across environments).
        label_key = self.node_instance_label_key
        if not all(k in metric for k in ("node", label_key)):
            return None

        try:
            data_to_validate = {
                "node": metric["node"],
                "instance_type": metric[label_key],
            }
            return NodeInstanceType(**data_to_validate)
        except ValidationError:
            return None

    def collect_range(
        self, start, end, step: Optional[str] = None, query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Query Prometheus `query_range` endpoint and return the raw series results.

        Returns the list under data.result or [] on error. If query is not
        provided, uses the standard CPU usage query. Falls back to a query
        without the 'container' label when needed.
        """
        if not self.base_url:
            logger.info("PROMETHEUS_URL is not set; attempting discovery.")
            self.is_available()

        if not self.base_url:
            logger.warning("PROMETHEUS_URL is not set and discovery failed. Skipping Prometheus range collection.")
            return []

        step = step or self.query_range_step
        q = query or self.cpu_usage_query

        base = self.base_url.rstrip("/")
        candidates = [
            f"{base}/api/v1/query_range",
            f"{base}/query_range",
            f"{base}/prometheus/api/v1/query_range",
        ]

        params = {"query": q, "start": None, "end": None, "step": step}

        # Accept aware or naive datetimes; normalize to Z-suffixed ISO
        try:
            params["start"] = start.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            params["end"] = end.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            # Fallback to string conversion
            params["start"] = str(start)
            params["end"] = str(end)

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)

        last_err = None
        for query_url in candidates:
            try:
                logger.info("Querying Prometheus range at %s", query_url)
                response = requests.get(
                    query_url,
                    params=params,
                    headers=headers,
                    auth=auth,
                    timeout=self.timeout,
                    verify=self.verify,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "success":
                    logger.warning(
                        "Prometheus range returned non-success for %s: %s",
                        query_url,
                        data.get("error", "Unknown"),
                    )
                    continue

                results = data.get("data", {}).get("result", [])
                # If no results and we used the container query, try fallback
                if not results and "container" in q:
                    fallback_query = (
                        f"sum(rate(container_cpu_usage_seconds_total[{self.query_range_step}]))"
                        " by (namespace, pod, node)"
                    )
                    logger.info("Falling back to containerless grouped CPU query for range")
                    return self.collect_range(start, end, step=step, query=fallback_query)

                logger.info("Prometheus range at %s returned %d series", query_url, len(results))
                return results
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.debug("Failed range query to Prometheus at %s: %s", query_url, e)
                continue
            except Exception as e:
                last_err = e
                logger.error(
                    "Unexpected error during Prometheus range query at %s: %s",
                    query_url,
                    e,
                )
                continue

        if last_err:
            logger.error("All Prometheus range endpoints failed. Last error: %s", last_err)
        else:
            logger.error("Prometheus range queries returned no results for query: %s", q)
        return []
