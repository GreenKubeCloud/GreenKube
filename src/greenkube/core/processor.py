# src/greenkube/core/processor.py

# --- Imports des utilitaires, modèles et collecteurs ---
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
from ..collectors.base_collector import BaseCollector
from ..collectors.node_collector import NodeCollector
from ..models.metrics import CombinedMetric, EnergyMetric, CostMetric
from .calculator import CarbonCalculator

class DataProcessor:
    def __init__(self, energy_collector: BaseCollector, cost_collector: BaseCollector, calculator: CarbonCalculator):
        """
        Initialise le processeur avec ses dépendances (collecteurs, calculateur).
        """
        self.node_collector = NodeCollector()
        self.energy_collector = energy_collector 
        self.cost_collector = cost_collector
        self.calculator = calculator

    def run(self):
        """
        Orchestre la collecte, la combinaison et le calcul des données.
        """
        print("INFO: Starting data processing cycle...")

        # --- Étape 1: Collecte des données brutes ---
        cloud_zones = self.node_collector.collect()
        if not cloud_zones:
            print("WARN: No cloud zones found. Cannot determine carbon intensity.")
            return []

        # Pour le MVP, on suppose une seule région pour tout le cluster
        emaps_zone = get_emaps_zone_from_cloud_zone(cloud_zones[0])
        
        energy_metrics: list[EnergyMetric] = self.energy_collector.collect()
        cost_metrics: list[CostMetric] = self.cost_collector.collect()

        if not energy_metrics:
            print("WARN: Energy collector returned no data.")
            return []
            
        # --- Étape 2: Optimisation de la recherche des coûts ---
        # On transforme la liste des coûts en dictionnaire pour un accès instantané.
        # La clé est le nom du pod.
        cost_map = {metric.pod_name: metric for metric in cost_metrics}

        # --- Étape 3: Combinaison et calcul des métriques ---
        combined_metrics = []
        for energy_metric in energy_metrics:
            # CORRECTION : On accède aux attributs directement (ex: .pod_name)
            # au lieu d'utiliser .get("pod_name")
            pod_name = energy_metric.pod_name
            
            # On cherche le coût correspondant dans notre dictionnaire optimisé
            cost_metric = cost_map.get(pod_name)
            total_cost = cost_metric.total_cost if cost_metric else 0.0

            # Le calculateur utilise les joules pour calculer les émissions
            # et récupère l'intensité carbone la plus récente de la BDD.
            carbon_result = self.calculator.calculate_emissions(
                joules=energy_metric.joules,
                zone=emaps_zone
            )

            combined_metrics.append(
                CombinedMetric(
                    pod_name=pod_name,
                    namespace=energy_metric.namespace,
                    total_cost=total_cost,
                    co2e_grams=carbon_result["co2e_grams"],
                    pue=carbon_result["pue"], # PUE utilisé pour le calcul
                    grid_intensity=carbon_result["grid_intensity"] # Intensité utilisée
                )
            )
        
        print(f"INFO: Processing complete. Found {len(combined_metrics)} combined metrics.")
        return combined_metrics

