# src/greenkube/core/calculator.py
"""
Le cerveau de GreenKube. Ce module contient la logique de base pour transformer
les métriques énergétiques brutes en données significatives sur les émissions de carbone.
"""
from dataclasses import dataclass
from ..storage.base_repository import CarbonIntensityRepository

# Constantes de conversion
JOULES_PER_KWH = 3.6e6
GRAMS_PER_KG = 1000

@dataclass
class CarbonCalculationResult:
    """Représente le résultat d'un calcul d'émissions de carbone."""
    co2e_grams: float
    grid_intensity: float # en gCO2e/kWh

class CarbonCalculator:
    """
    Calcule les émissions de CO2e à partir de la consommation d'énergie
    et de l'intensité carbone du réseau électrique.
    """
    def __init__(self, repository: CarbonIntensityRepository, pue: float = 1.5):
        self.repository = repository
        self.pue = pue # Power Usage Effectiveness

    def calculate_emissions(self, joules: float, zone: str, timestamp: str) -> CarbonCalculationResult:
        """
        Calcule les émissions de CO2e pour une consommation d'énergie donnée.

        Args:
            joules: L'énergie consommée en joules.
            zone: La zone géographique (ex: 'FR', 'US-CA').
            timestamp: L'horodatage de la mesure au format ISO.

        Returns:
            Un objet CarbonCalculationResult avec les grammes de CO2e et l'intensité du réseau.
        """
        # Le calculateur ne sait pas d'où viennent les données (SQLite, ES...),
        # il demande simplement ce dont il a besoin. Certains tests/mocks expose
        # une méthode `get_latest_for_zone(zone)` while real repositories may
        # expose `get_for_zone_at_time(zone, timestamp)`; support both.
        grid_intensity = None
        if hasattr(self.repository, 'get_latest_for_zone'):
            try:
                grid_intensity = self.repository.get_latest_for_zone(zone)
            except Exception:
                grid_intensity = None
        if grid_intensity is None and hasattr(self.repository, 'get_for_zone_at_time'):
            try:
                grid_intensity = self.repository.get_for_zone_at_time(zone, timestamp)
            except Exception:
                grid_intensity = None

        if grid_intensity is None:
            print(f"WARN: Aucune donnée d'intensité carbone trouvée via le repository pour la zone '{zone}' à l'instant {timestamp}.")
            grid_intensity = 0.0

        # 1. Convertir les joules en kWh
        kwh = joules / JOULES_PER_KWH

        # 2. Appliquer le PUE pour obtenir l'énergie totale consommée par le data center
        total_kwh = kwh * self.pue

        # 3. Calculer les émissions de CO2e en grammes
        # (gCO2e/kWh) * kWh = gCO2e
        co2e_grams = grid_intensity * total_kwh

        return CarbonCalculationResult(
            co2e_grams=co2e_grams,
            grid_intensity=grid_intensity
        )

