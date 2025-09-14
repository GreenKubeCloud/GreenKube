# tests/collectors/test_opencost_collector.py
"""
Unit tests for the OpenCostCollector.
"""
import unittest
from unittest.mock import patch, MagicMock
import requests

from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.models.metrics import CostMetric

# Source de vérité pour les données de test.
# Si le format de l'API change, on ne modifie que cet objet.
MOCK_API_RESPONSE = {
    "code": 200,
    "data": [
        {
            "alertmanager-main-0": {
                "name": "alertmanager-main-0",
                "properties": {
                    "cluster": "default-cluster",
                    "namespace": "monitoring",
                    "pod": "alertmanager-main-0"
                },
                "totalCost": 0.00063,
                "cpuCost": 0.00026,
                "ramCost": 0.00037
            },
            "coredns-674b8bbfcf-fvw4g": {
                "name": "coredns-674b8bbfcf-fvw4g",
                "properties": {
                    "cluster": "default-cluster",
                    "namespace": "kube-system",
                    "pod": "coredns-674b8bbfcf-fvw4g"
                },
                "totalCost": 0.00204,
                "cpuCost": 0.00187,
                "ramCost": 0.00017
            }
        }
    ]
}


class TestOpenCostCollector(unittest.TestCase):
    """
    Test suite for the OpenCostCollector.
    """

    @patch('greenkube.collectors.opencost_collector.requests.get')
    def test_collect_parses_data_dynamically(self, mock_get):
        """
        Tests that the collect method correctly parses the API response.
        This test is ROBUST: it validates the transformation logic without
        hardcoding specific values from the mock data in the assertions.
        """
        # 1. Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = MOCK_API_RESPONSE
        mock_get.return_value = mock_response

        collector = OpenCostCollector()

        # 2. Act
        results = collector.collect()

        # 3. Assert
        mock_data_items = MOCK_API_RESPONSE["data"][0]
        # Vérifier que le nombre de métriques créées correspond au nombre d'entrées
        self.assertEqual(len(results), len(mock_data_items))

        # Créer un dictionnaire pour retrouver facilement les résultats
        results_map = {metric.pod_name: metric for metric in results}

        # Itérer sur les données de test et vérifier chaque transformation
        for pod_id, mock_item_data in mock_data_items.items():
            expected_pod_name = mock_item_data["properties"]["pod"]
            self.assertIn(expected_pod_name, results_map, f"Le pod '{expected_pod_name}' est manquant dans les résultats")
            
            result_metric = results_map[expected_pod_name]
            
            # Valider la transformation
            self.assertEqual(result_metric.namespace, mock_item_data["properties"]["namespace"])
            self.assertAlmostEqual(result_metric.total_cost, mock_item_data.get("totalCost", 0.0))
            self.assertAlmostEqual(result_metric.cpu_cost, mock_item_data.get("cpuCost", 0.0))
            self.assertAlmostEqual(result_metric.ram_cost, mock_item_data.get("ramCost", 0.0))
        
        print("\nTestOpenCostCollector (dynamic): All assertions passed! ✅")

    @patch('greenkube.collectors.opencost_collector.requests.get')
    def test_collect_skips_items_with_missing_namespace(self, mock_get):
        """
        Tests that the collector gracefully skips an entry if its namespace is missing.
        """
        # 1. Arrange
        MALFORMED_RESPONSE = {
            "code": 200, "data": [{"pod-good": {"properties": {"namespace": "good-ns", "pod": "pod-good"}, "totalCost": 1.0},
                                 "pod-bad": {"properties": {"pod": "pod-bad"}, "totalCost": 2.0}}] # Pas de namespace
        }
        mock_response = MagicMock()
        mock_response.json.return_value = MALFORMED_RESPONSE
        mock_get.return_value = mock_response
        collector = OpenCostCollector()

        # 2. Act
        results = collector.collect()

        # 3. Assert
        # Seule l'entrée valide doit être conservée
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].pod_name, "pod-good")

    @patch('greenkube.collectors.opencost_collector.requests.get')
    def test_collect_handles_api_error_gracefully(self, mock_get):
        """
        Tests that the collector returns an empty list when the API call fails.
        """
        # 1. Arrange
        mock_get.side_effect = requests.exceptions.RequestException("API is down")
        collector = OpenCostCollector()

        # 2. Act
        results = collector.collect()

        # 3. Assert
        # Le collecteur doit retourner une liste vide en cas d'erreur
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 0)

