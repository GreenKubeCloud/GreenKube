# tests/core/test_calculator.py
"""
Unit tests for the CarbonCalculator.
"""
import unittest
from greenkube.core.calculator import CarbonCalculator
from greenkube.models.metrics import EnergyMetric, CarbonEmissionMetric

class TestCarbonCalculator(unittest.TestCase):
    """
    Test suite for the CarbonCalculator.
    """

    def test_calculate_carbon_emissions(self):
        """
        Tests the core carbon calculation logic with a known input.
        """
        # 1. Arrange
        # Create a calculator with known PUE and grid intensity for a predictable result
        calculator = CarbonCalculator(pue=1.5, grid_intensity_gco2e_per_kwh=50.0)
        
        # Create a sample energy metric. 3,600,000 Joules is exactly 1 kWh.
        sample_metrics = [
            EnergyMetric(pod_name="test-pod", namespace="test-ns", joules=3600000.0)
        ]

        # 2. Act
        results = calculator.calculate_carbon_emissions(sample_metrics)

        # 3. Assert
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)
        
        first_result = results[0]
        self.assertIsInstance(first_result, CarbonEmissionMetric)
        
        # Manually calculate the expected result:
        # energy_kwh = 3,600,000 / 3,600,000 = 1.0 kWh
        # total_energy_kwh = 1.0 kWh * 1.5 PUE = 1.5 kWh
        # co2e_grams = 1.5 kWh * 50 gCO2e/kWh = 75.0 grams
        expected_co2e_grams = 75.0
        
        self.assertAlmostEqual(first_result.co2e_grams, expected_co2e_grams, places=5)
        print("\nTestCarbonCalculator: All assertions passed! ✅")

