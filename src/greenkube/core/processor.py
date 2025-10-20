# src/greenkube/core/processor.py

from ..models.metrics import CombinedMetric
from ..collectors.kepler_collector import KeplerCollector
from ..collectors.opencost_collector import OpenCostCollector
from ..collectors.node_collector import NodeCollector
from ..core.calculator import CarbonCalculator
from ..utils.mapping_translator import oci_region_to_electricity_maps_zone

class DataProcessor:
    """
    Orchestre la collecte de données, le traitement et le calcul des émissions de carbone.
    """
    def __init__(self, energy_collector, cost_collector, calculator: CarbonCalculator):
        # Use generic names to match tests and CLI expectations
        self.energy_collector = energy_collector
        self.cost_collector = cost_collector
        self.calculator = calculator
        self.node_collector = NodeCollector()

    def run(self) -> list[CombinedMetric]:
        """
        Exécute le pipeline de traitement des données.
        """
        print("INFO: Starting data processing cycle...")
        
        # 1. Obtenir les zones des nœuds du cluster
        cloud_zones = self.node_collector.collect()
        if not cloud_zones:
            print("WARN: Could not determine node zones. Carbon intensity will be 0.")
            return []

        # Pour l'instant, on suppose que tous les nœuds sont dans la même zone.
        # C'est une simplification qui devra être améliorée.
        emaps_zone = oci_region_to_electricity_maps_zone(cloud_zones[0])

        # 2. Collecter les métriques d'énergie et de coût
        energy_metrics = self.energy_collector.collect()
        cost_metrics = self.cost_collector.collect()

        # 3. Créer un dictionnaire pour un accès rapide aux coûts par pod
        cost_map = {metric.pod_name: metric for metric in cost_metrics}

        # 4. Combiner les données
        combined_metrics = []
        for energy_metric in energy_metrics:
            pod_name = energy_metric.pod_name
            if pod_name in cost_map:
                cost_metric = cost_map[pod_name]
                total_cost = cost_metric.total_cost

                # Le calculateur utilise les joules pour calculer les émissions
                # et récupère l'intensité carbone la plus récente de la BDD.
                carbon_result = self.calculator.calculate_emissions(
                    joules=energy_metric.joules,
                    zone=emaps_zone,
                    timestamp=energy_metric.timestamp.isoformat()
                )

                combined_metrics.append(
                    CombinedMetric(
                        pod_name=pod_name,
                        namespace=energy_metric.namespace,
                        total_cost=total_cost,
                        co2e_grams=carbon_result.co2e_grams,
                        pue=self.calculator.pue,
                        grid_intensity=carbon_result.grid_intensity
                    )
                )

        print(f"INFO: Processing complete. Found {len(combined_metrics)} combined metrics.")
        return combined_metrics

