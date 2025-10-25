# src/greenkube/collectors/opencost_collector.py
"""
This module contains the collector responsible for gathering cost
allocation data from the OpenCost API.
"""
import requests
from typing import List
from datetime import datetime, timezone
import logging

from .base_collector import BaseCollector
from ..models.metrics import CostMetric

logger = logging.getLogger(__name__)

class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service via an Ingress.
    """
    OPENCOST_API_URL = "https://opencost.greenkube.cloud/allocation/compute"

    def collect(self) -> List[CostMetric]:
        """
        Fetches cost data from OpenCost by making an HTTP request to its API.

        Returns:
            A list of CostMetric objects, or an empty list if an error occurs.
        """
        logger.info("Collecting data from OpenCostCollector (using Ingress)...")

        params = {
            "window": "1d",
            "aggregate": "pod"
        }

        try:
            import warnings
            from requests.packages.urllib3.exceptions import InsecureRequestWarning
            
            warnings.simplefilter('ignore', InsecureRequestWarning)
            
            response = requests.get(self.OPENCOST_API_URL, params=params, timeout=10, verify=False)
            
            response.raise_for_status()

            try:
                response_data = response.json().get("data")
            except requests.exceptions.JSONDecodeError:
                logger.error("Failed to decode JSON. Server sent non-JSON response.")
                logger.debug("Raw response content: %s", response.text[:500])
                return []

            if not response_data or not isinstance(response_data, list) or len(response_data) == 0:
                logger.warning("OpenCost API returned no data. This can happen if the cluster is new.")
                return []
            
            cost_data = response_data[0]

        except requests.exceptions.RequestException as e:
            logger.error("Could not connect to OpenCost API via Ingress: %s", e)
            return []

        # --- TRAITEMENT DE LA RÉPONSE (CORRIGÉ) ---
        collected_metrics = []
        now = datetime.now(timezone.utc)
        
        for resource_id, item in cost_data.items():
            # Le nom du pod et le namespace sont à l'intérieur de l'objet 'properties'.
            properties = item.get("properties", {})
            pod_name = properties.get("pod")
            namespace = properties.get("namespace")

            # Si la propriété "pod" n'existe pas, on utilise la clé comme nom de pod.
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
                timestamp=now
            )
            collected_metrics.append(metric)

        logger.info("Successfully collected %d metrics from OpenCost.", len(collected_metrics))
        return collected_metrics

