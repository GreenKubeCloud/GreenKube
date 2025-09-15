# src/greenkube/core/calculator.py
"""
Le cerveau de GreenKube. Ce module contient la logique de base pour transformer
les métriques énergétiques brutes en données significatives sur les émissions de carbone.
"""
# On importe l'interface, pas l'implémentation concrète
from ..storage.base_repository import CarbonIntensityRepository

# Facteur de conversion des Joules en kilowatt-heures (kWh)
JOULES_PER_KWH = 3_600_000
DEFAULT_PUE = 1.5

class CarbonCalculator:
    """
    Calcule les émissions de carbone en utilisant un Repository pour l'accès aux données.
    """
    def __init__(self, repository: CarbonIntensityRepository):
        """
        Initialise le calculateur avec un repository pour les données environnementales.
        """
        self.repository = repository

    def calculate_emissions(self, joules: float, zone: str) -> dict:
        """
        Calcule les émissions de CO2e pour une seule valeur de consommation d'énergie.
        """
        # Le calculateur ne sait pas d'où viennent les données (SQLite, ES...),
        # il demande simplement ce dont il a besoin.
        grid_intensity = self.repository.get_latest_for_zone(zone)
        if grid_intensity is None:
            print(f"WARN: Aucune donnée d'intensité carbone trouvée via le repository pour la zone '{zone}'.")
            grid_intensity = 0.0

        pue = DEFAULT_PUE
        energy_kwh = joules / JOULES_PER_KWH
        total_energy_kwh = energy_kwh * pue
        co2e_grams = total_energy_kwh * grid_intensity

        return {
            "co2e_grams": co2e_grams,
            "grid_intensity": grid_intensity,
            "pue": pue
        }

