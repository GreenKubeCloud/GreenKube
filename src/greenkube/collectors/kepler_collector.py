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
        """
        print("INFO: Collecting data from KeplerCollector (using realistic mocked data)...")

        # --- DONNÉES MOCK MISES À JOUR ---
        # Ces données utilisent des noms de pods et de namespaces réels de votre cluster
        # pour permettre une fusion correcte avec les données d'OpenCost.
        mock_api_response = [
            {
                "pod_name": "prometheus-k8s-0", 
                "namespace": "monitoring", 
                "joules": 4500000.0, # ~1.25 kWh
                "node": "node-1", 
                "region": "us-east-1"
            },
            {
                "pod_name": "grafana-7c68d76c67-6ljpv", 
                "namespace": "monitoring", 
                "joules": 900000.0, # ~0.25 kWh
                "node": "node-1", 
                "region": "us-east-1"
            },
            {
                "pod_name": "coredns-674b8bbfcf-fvw4g", 
                "namespace": "kube-system", 
                "joules": 18000.0, 
                "node": "node-2", 
                "region": "eu-west-1"
            },
            {
                "pod_name": "argocd-server-64d5fcbd58-t64p2", 
                "namespace": "argocd", 
                "joules": 7200.0, 
                "node": "node-2", 
                "region": "eu-west-1"
            },
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

