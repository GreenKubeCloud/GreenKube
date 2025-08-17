# tests/core/test_processor.py
"""
Unit tests for the DataProcessor.
"""
import unittest
from greenkube.core.processor import DataProcessor
from greenkube.collectors.kepler_collector import KeplerCollector
from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.calculator import CarbonCalculator
from greenkube.models.metrics import CombinedMetric

class TestDataProcessor(unittest.TestCase):
    """
    Test suite for the DataProcessor.
    """
    def test_run_pipeline_with_multi_region_data(self):
        """
        Tests that the processor correctly runs the full pipeline and
        combines data, including detailed environmental metrics, from a
        simulated multi-region environment.
        """
        # 1. Arrange
        processor = DataProcessor(
            energy_collector=KeplerCollector(),
            cost_collector=OpenCostCollector(),
            calculator=CarbonCalculator()
        )

        # 2. Act
        results = processor.run()

        # 3. Assert
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 4)

        # Check a pod from the "us-east-1" region (higher carbon)
        backend_pod = next(item for item in results if item.pod_name == "backend-xyz")
        self.assertEqual(backend_pod.total_cost, 1.15)
        # Expected: (3.6M Joules / 3.6M) * 1.6 PUE * 450 g/kWh = 720.0 grams
        self.assertAlmostEqual(backend_pod.co2e_grams, 720.0, places=5)
        # Assert the new detailed fields are correct
        self.assertEqual(backend_pod.pue, 1.6)
        self.assertEqual(backend_pod.grid_intensity, 450.0)

        # Check a pod from the "eu-west-1" region (greener)
        auth_pod = next(item for item in results if item.pod_name == "auth-service-fgh")
        self.assertEqual(auth_pod.total_cost, 0.65)
        # Expected: (1500.7 Joules / 3.6M) * 1.2 PUE * 50 g/kWh = 0.0250 grams
        self.assertAlmostEqual(auth_pod.co2e_grams, 0.0250, places=4)
        # Assert the new detailed fields are correct
        self.assertEqual(auth_pod.pue, 1.2)
        self.assertEqual(auth_pod.grid_intensity, 50.0)
        
        print("\nTestDataProcessor: All assertions passed! ✅")