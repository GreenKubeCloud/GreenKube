# tests/collectors/test_opencost_collector.py
"""
Unit tests for the OpenCostCollector.
"""
import unittest
from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.models.metrics import CostMetric

class TestOpenCostCollector(unittest.TestCase):
    """
    Test suite for the OpenCostCollector.
    """

    def test_collect_returns_list_of_cost_metrics(self):
        """
        Tests that the collect method returns a list of CostMetric objects
        and that the data is structured as expected.
        """
        # 1. Arrange
        collector = OpenCostCollector()

        # 2. Act
        results = collector.collect()

        # 3. Assert
        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)
        
        first_result = results[0]
        self.assertIsInstance(first_result, CostMetric)
        
        self.assertEqual(first_result.pod_name, "frontend-abc")
        self.assertEqual(first_result.namespace, "e-commerce")
        self.assertEqual(first_result.total_cost, 0.55)
        print("\nTestOpenCostCollector: All assertions passed! ✅")

