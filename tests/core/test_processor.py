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
    def test_run_pipeline(self):
        """
        Tests that the processor correctly runs the pipeline and combines data.
        """
        # 1. Arrange
        # Use our real mocked collectors and calculator
        processor = DataProcessor(
            energy_collector=KeplerCollector(),
            cost_collector=OpenCostCollector(),
            calculator=CarbonCalculator(pue=1.5, grid_intensity_gco2e_per_kwh=50.0)
        )

        # 2. Act
        results = processor.run()

        # 3. Assert
        self.assertIsInstance(results, list)
        self.assertTrue(all(isinstance(item, CombinedMetric) for item in results))
        self.assertEqual(len(results), 4)

        # Check a specific, predictable result (backend-xyz pod)
        backend_pod_metric = next(item for item in results if item.pod_name == "backend-xyz")
        self.assertEqual(backend_pod_metric.total_cost, 1.15)
        self.assertAlmostEqual(backend_pod_metric.co2e_grams, 75.0, places=5)
        print("\nTestDataProcessor: All assertions passed! ✅")

