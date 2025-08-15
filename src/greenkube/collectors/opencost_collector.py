# src/greenkube/collectors/opencost_collector.py
"""
This module contains the collector responsible for gathering cost
allocation data from the OpenCost API.
"""
from typing import List
from datetime import datetime, timezone

from .base_collector import BaseCollector
from ..models.metrics import CostMetric

class OpenCostCollector(BaseCollector):
    """
    Collects cost allocation data from an OpenCost service.

    For this initial version, it returns mocked data.
    """
    def collect(self) -> List[CostMetric]:
        """
        Fetches cost data from OpenCost.

        In a real implementation, this method would make an HTTP request to the
        OpenCost API, parse the response, and instantiate CostMetric objects.

        Returns:
            A list of CostMetric objects.
        """
        print("INFO: Collecting data from OpenCostCollector (using mocked data)...")

        # --- MOCKED DATA ---
        # This simulates the kind of raw data we might get from OpenCost's API
        mock_api_response = [
            {"pod_name": "frontend-abc", "namespace": "e-commerce", "cpu_cost": 0.25, "ram_cost": 0.30, "total_cost": 0.55},
            {"pod_name": "backend-xyz", "namespace": "e-commerce", "cpu_cost": 0.50, "ram_cost": 0.65, "total_cost": 1.15},
            {"pod_name": "database-123", "namespace": "e-commerce", "cpu_cost": 0.80, "ram_cost": 1.20, "total_cost": 2.00},
            {"pod_name": "auth-service-fgh", "namespace": "security", "cpu_cost": 0.30, "ram_cost": 0.35, "total_cost": 0.65},
        ]

        collected_metrics = [
            CostMetric(
                pod_name=item["pod_name"],
                namespace=item["namespace"],
                cpu_cost=item["cpu_cost"],
                ram_cost=item["ram_cost"],
                total_cost=item["total_cost"],
                timestamp=datetime.now(timezone.utc)
            )
            for item in mock_api_response
        ]

        print(f"INFO: Successfully collected {len(collected_metrics)} metrics from OpenCost.")
        return collected_metrics
