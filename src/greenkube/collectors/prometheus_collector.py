# src/greenkube/collectors/prometheus_collector.py

"""
PrometheusCollector fetches CPU usage and node instance type metrics.
This data is the input for the BasicEstimator.
"""
import requests
import logging
from typing import List, Dict, Any, Optional
from pydantic import ValidationError

from greenkube.collectors.base_collector import BaseCollector
from greenkube.core.config import Config
from greenkube.models.prometheus_metrics import PrometheusMetric, PodCPUUsage, NodeInstanceType

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
        self.base_url = settings.PROMETHEUS_URL
        self.query_range_step = settings.PROMETHEUS_QUERY_RANGE_STEP
        self.timeout = 10

        # TLS verify and auth support
        self.verify = getattr(settings, 'PROMETHEUS_VERIFY_CERTS', True)
        self.bearer_token = getattr(settings, 'PROMETHEUS_BEARER_TOKEN', None)
        self.username = getattr(settings, 'PROMETHEUS_USERNAME', None)
        self.password = getattr(settings, 'PROMETHEUS_PASSWORD', None)

        # Query for average CPU usage in cores over the last step.
        # We filter for containers with a name, which is standard.
        self.cpu_usage_query = (
            f"sum(rate(container_cpu_usage_seconds_total{{container!=''}}[{self.query_range_step}])) "
            "by (namespace, pod, container, node)"
        )

        # Prometheus label key used to map node -> instance type may vary across setups.
        self.node_instance_label_key = getattr(settings, 'PROMETHEUS_NODE_INSTANCE_LABEL', 'label_node_kubernetes_io_instance_type')
        # Build a query that filters nodes which have the configured instance-type label.
        self.node_labels_query = f"kube_node_labels{{{self.node_instance_label_key}!=''}}"

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
                f"sum(rate(container_cpu_usage_seconds_total[{self.query_range_step}]))"
                " by (namespace, pod, node)"
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
                "No node instance-type labels found in Prometheus using label '%s'; estimator will use default instance profile for unknown nodes.",
                self.node_instance_label_key,
            )

        if malformed_cpu_count:
            logger.warning("Skipped %d malformed CPU metric item(s). Examples: %s", malformed_cpu_count, malformed_cpu_examples)

        if non_pod_skipped:
            logger.info("Skipped %d non-pod Prometheus series during CPU query (these are node-level or aggregate series).", non_pod_skipped)

        if malformed_node_count:
            logger.warning("Skipped %d malformed node metric item(s). Examples: %s", malformed_node_count, malformed_node_examples)

        return PrometheusMetric(
            pod_cpu_usage=parsed_cpu_usage,
            node_instance_types=parsed_node_types,
        )

    def _query_prometheus(self, query: str) -> List[Dict[str, Any]]:
        """
        Internal helper to run a query against the Prometheus API.
        
        Returns the 'result' list from the JSON response, or [] on failure.
        """
        if not self.base_url:
            logger.warning("PROMETHEUS_URL is not set. Skipping Prometheus collection.")
            return []

        # Normalize base URL and try a couple of common endpoint forms.
        base = self.base_url.rstrip('/')
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
        return []

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
        if not all(k in metric for k in ("namespace", "pod", "container", "node")):
            return None

        try:
            data_to_validate = {
                "namespace": metric["namespace"],
                "pod": metric["pod"],
                "container": metric["container"],
                "node": metric["node"],
                "cpu_usage_cores": float(value_str)
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
        if not all(k in metric for k in ("namespace", "pod", "node")):
            return None

        try:
            data_to_validate = {
                "namespace": metric["namespace"],
                "pod": metric["pod"],
                "container": metric.get("container", ""),
                "node": metric["node"],
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
                "instance_type": metric[label_key]
            }
            return NodeInstanceType(**data_to_validate)
        except ValidationError:
            return None

