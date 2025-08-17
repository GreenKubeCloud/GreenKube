# src/greenkube/collectors/kepler_collector.py
"""
This module contains the collector responsible for gathering energy
consumption data from the Kepler API. Kepler provides highly granular,
pod-level energy metrics by leveraging eBPF.
"""
from typing import List
from datetime import datetime, timezone

# Import the base class and the model from our other files
from .base_collector import BaseCollector
from ..models.metrics import EnergyMetric

class KeplerCollector(BaseCollector):
    """
    Collects energy consumption data from a Kepler service.

    For this initial version, it returns mocked data to allow for development
    and testing without a live Kepler deployment.
    """
    def collect(self) -> List[EnergyMetric]:
        """
        Fetches energy data from Kepler.

        In a real implementation, this method would make an HTTP request to the
        Kepler Prometheus-compatible API endpoint, parse the response, and
        instantiate EnergyMetric objects.

        Returns:
            A list of EnergyMetric objects representing the latest energy
            consumption data for various pods.
        """
        print("INFO: Collecting data from KeplerCollector (using mocked data)...")

        # --- MOCKED DATA ---
        # This simulates data from a cluster spanning two regions
        mock_api_response = [
            {"pod_name": "frontend-abc", "namespace": "e-commerce", "joules": 1250.5, "node": "node-1", "region": "us-east-1"},
            {"pod_name": "backend-xyz", "namespace": "e-commerce", "joules": 3600000.0, "node": "node-1", "region": "us-east-1"},
            {"pod_name": "database-123", "namespace": "e-commerce", "joules": 8950.2, "node": "node-2", "region": "eu-west-1"},
            {"pod_name": "auth-service-fgh", "namespace": "security", "joules": 1500.7, "node": "node-2", "region": "eu-west-1"},
        ]

        collected_metrics = [
            EnergyMetric(
                pod_name=item["pod_name"],
                namespace=item["namespace"],
                joules=item["joules"],
                timestamp=datetime.now(timezone.utc),
                node=item["node"],
                region=item["region"]
            )
            for item in mock_api_response
        ]

        print(f"INFO: Successfully collected {len(collected_metrics)} metrics from Kepler.")
        return collected_metrics
