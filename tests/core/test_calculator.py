# tests/core/test_calculator.py
import unittest
from unittest.mock import MagicMock
from src.greenkube.core.calculator import CarbonCalculator
from src.greenkube.models.metrics import EnergyMetric

class TestCarbonCalculator(unittest.TestCase):
    """
    Test suite for the CarbonCalculator.
    """

    def test_calculate_emissions(self):
        """
        Tests the carbon calculation with a mocked repository.
        """
        # 1. Arrange
        # Créer un faux repository qui retourne des valeurs contrôlées
        mock_repo = MagicMock()
        mock_repo.get_latest_for_zone.side_effect = lambda zone: 500.0 if zone == "us-east-1" else 50.0

        # Injecter le faux repository dans le calculateur
        calculator = CarbonCalculator(repository=mock_repo)
        
        # 2. Act
        result_us = calculator.calculate_emissions(joules=3600000.0, zone="us-east-1") # 1 kWh
        result_eu = calculator.calculate_emissions(joules=7200000.0, zone="eu-west-1") # 2 kWh

        # 3. Assert
        # Attendu pour us-east-1: (1 kWh * 1.5 PUE) * 500 g/kWh = 750.0 grams
        self.assertAlmostEqual(result_us["co2e_grams"], 750.0)
        
        # Attendu pour eu-west-1: (2 kWh * 1.5 PUE) * 50 g/kWh = 150.0 grams
        self.assertAlmostEqual(result_eu["co2e_grams"], 150.0)

        print("\nTestCarbonCalculator: All assertions passed! ✅")
