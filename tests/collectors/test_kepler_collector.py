# tests/collectors/test_kepler_collector.py
"""
Unit tests for the KeplerCollector to ensure it functions correctly.
"""
import unittest
from greenkube.collectors.kepler_collector import KeplerCollector
from greenkube.models.metrics import EnergyMetric

class TestKeplerCollector(unittest.TestCase):
    """
    Test suite for the KeplerCollector.
    """

    def test_collect_returns_list_of_energy_metrics(self):
        """
        Tests that the collect method returns a list of EnergyMetric objects
        and that the data is structured as expected.
        """
        # 1. Arrange: Create an instance of the collector
        collector = KeplerCollector()

        # 2. Act: Call the method we want to test
        results = collector.collect()

        # 3. Assert: Check that the results are correct
        self.assertIsInstance(results, list, "Should return a list")
        self.assertTrue(len(results) > 0, "The list should not be empty")
        
        # Check the first item in the list for correctness
        first_result = results[0]
        self.assertIsInstance(first_result, EnergyMetric, "All items should be EnergyMetric objects")
        
        # Verify the data types and values of the first mocked item
        self.assertEqual(first_result.pod_name, "frontend-abc")
        self.assertEqual(first_result.namespace, "e-commerce")
        self.assertEqual(first_result.joules, 1250.5)
        print("\nTestKeplerCollector: All assertions passed! ✅")

# This allows running the test directly from the command line
if __name__ == '__main__':
    unittest.main()
