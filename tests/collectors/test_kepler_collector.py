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

    def test_collect_returns_valid_energy_metrics(self):
        """
        Tests that the collect method returns a non-empty list of EnergyMetric objects
        with valid attributes.
        """
        # 1. Arrange: Create an instance of the collector
        collector = KeplerCollector()

        # 2. Act: Call the method we want to test
        results = collector.collect()

        # 3. Assert: Check that the results are correct
        # Assert: Should return a non-empty list
        self.assertIsInstance(results, list, "Should return a list")
        self.assertGreater(len(results), 0, "The list should not be empty")

        # All items should be EnergyMetric and have required attributes
        for metric in results:
            self.assertIsInstance(metric, EnergyMetric)
            self.assertIsInstance(metric.pod_name, str)
            self.assertIsInstance(metric.namespace, str)
            self.assertIsInstance(metric.joules, float)
            self.assertIsInstance(metric.timestamp, object)
            self.assertIsInstance(metric.node, str)
            self.assertIsInstance(metric.region, str)

        # Optionally: Check that the pod names match the mock data
        expected_pods = {
            "prometheus-k8s-0",
            "grafana-7c68d76c67-6ljpv",
            "coredns-674b8bbfcf-fvw4g",
            "argocd-server-64d5fcbd58-t64p2",
        }
        actual_pods = {metric.pod_name for metric in results}
        self.assertEqual(actual_pods, expected_pods)

        # Optionally: Check the number of metrics matches the mock data
        self.assertEqual(len(results), 4)

        print("\nTestKeplerCollector: All assertions passed! ✅")

# This allows running the test directly from the command line
if __name__ == '__main__':
    unittest.main()
