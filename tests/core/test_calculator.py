# tests/core/test_calculator.py
"""
Unit tests for the CarbonCalculator.
"""
import unittest
from greenkube.core.calculator import CarbonCalculator
from greenkube.models.metrics import EnergyMetric, CarbonEmissionMetric, EnvironmentalMetric

class TestCarbonCalculator(unittest.TestCase):
    """
    Test suite for the CarbonCalculator.
    """

    def test_calculate_carbon_emissions_multi_region(self):
        """
        Tests the carbon calculation with inputs from multiple regions,
        ensuring the correct environmental data is used for each.
        """
        # 1. Arrange
        calculator = CarbonCalculator()
        
        # Sample energy metrics from two different regions
        sample_metrics = [
            EnergyMetric(pod_name="pod-us", namespace="test-ns", joules=3600000.0, region="us-east-1"), # 1 kWh
            EnergyMetric(pod_name="pod-eu", namespace="test-ns", joules=7200000.0, region="eu-west-1")  # 2 kWh
        ]
        
        # Corresponding environmental data for each region
        environmental_data = {
            "us-east-1": EnvironmentalMetric(pue=1.5, grid_intensity=500.0),
            "eu-west-1": EnvironmentalMetric(pue=1.2, grid_intensity=50.0)
        }

        # 2. Act
        results = calculator.calculate_carbon_emissions(sample_metrics, environmental_data)

        # 3. Assert
        self.assertEqual(len(results), 2)
        
        result_us = next(r for r in results if r.pod_name == "pod-us")
        result_eu = next(r for r in results if r.pod_name == "pod-eu")
        
        # Expected for us-east-1: (1 kWh * 1.5 PUE) * 500 g/kWh = 750.0 grams
        self.assertAlmostEqual(result_us.co2e_grams, 750.0, places=5)
        
        # Expected for eu-west-1: (2 kWh * 1.2 PUE) * 50 g/kWh = 120.0 grams
        self.assertAlmostEqual(result_eu.co2e_grams, 120.0, places=5)

        print("\nTestCarbonCalculator: All assertions passed! ✅")