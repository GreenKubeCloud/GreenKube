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
        self.timeout = 10  # Connection timeout in seconds

        # Query for average CPU usage in cores over the last step.
        # We filter for containers with a name, which is standard.
        self.cpu_usage_query = (
            f"sum(rate(container_cpu_usage_seconds_total{{container!=''}}[{self.query_range_step}])) "
            "by (namespace, pod, container, node)"
        )
        
        # Query for node labels to find the instance type.
        # We only get nodes that *have* the instance type label.
        self.node_labels_query = "kube_node_labels{label_node_kubernetes_io_instance_type!=''}"

    def collect(self) -> PrometheusMetric:
        """
        Fetch all required metrics from Prometheus.
        
        Returns a PrometheusMetric object containing lists of parsed data.
        """
        cpu_results = self._query_prometheus(self.cpu_usage_query)
        node_results = self._query_prometheus(self.node_labels_query)

        parsed_cpu_usage = []
        for item in cpu_results:
            parsed_item = self._parse_cpu_data(item)
            if parsed_item:
                parsed_cpu_usage.append(parsed_item)

        parsed_node_types = []
        for item in node_results:
            parsed_item = self._parse_node_data(item)
            if parsed_item:
                parsed_node_types.append(parsed_item)

        return PrometheusMetric(
            pod_cpu_usage=parsed_cpu_usage,
            node_instance_types=parsed_node_types
        )

    def _query_prometheus(self, query: str) -> List[Dict[str, Any]]:
        """
        Internal helper to run a query against the Prometheus API.
        
        Returns the 'result' list from the JSON response, or [] on failure.
        """
        if not self.base_url:
            logger.warning("PROMETHEUS_URL is not set. Skipping Prometheus collection.")
            return []

        query_url = f"{self.base_url}/api/v1/query"
        params = {'query': query}

        try:
            response = requests.get(query_url, params=params, timeout=self.timeout)
            response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)

            data = response.json()

            if data.get('status') != 'success':
                logger.error(f"Prometheus API error for query '{query}': {data.get('error', 'Unknown error')}")
                return []

            return data.get('data', {}).get('result', [])

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to Prometheus at {query_url}: {e}")
            return []
        except Exception as e:
            logger.error(f"An unexpected error occurred during Prometheus query: {e}")
            return []

    def _parse_cpu_data(self, item: Dict[str, Any]) -> Optional[PodCPUUsage]:
        """
        Parses a single item from the CPU query result.
        Returns a PodCPUUsage model or None if parsing fails.
        """
        try:
            metric = item.get("metric", {})
            value_str = item.get("value", [None, None])[1]

            # Skip if value is NaN
            if value_str == "NaN":
                return None

            # This will raise KeyError if any key is missing,
            # which is caught by the except block.
            data_to_validate = {
                "namespace": metric["namespace"],
                "pod": metric["pod"],
                "container": metric["container"],
                "node": metric["node"],
                "cpu_usage_cores": float(value_str)
            }
            
            return PodCPUUsage(**data_to_validate)

        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as e:
            logger.warning(f"Skipping malformed CPU metric item '{item}': {e}")
            return None

    def _parse_node_data(self, item: Dict[str, Any]) -> Optional[NodeInstanceType]:
        """
        Parses a single item from the node labels query result.
        Returns a NodeInstanceType model or None if parsing fails.
        """
        try:
            metric = item.get("metric", {})

            # This will raise KeyError if any key is missing.
            data_to_validate = {
                "node": metric["node"],
                "instance_type": metric["label_node_kubernetes_io_instance_type"]
            }

            return NodeInstanceType(**data_to_validate)
            
        except (KeyError, ValidationError) as e:
            logger.warning(f"Skipping malformed node metric item '{item}': {e}")
            return None

