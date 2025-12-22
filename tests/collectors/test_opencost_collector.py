# tests/collectors/test_opencost_collector.py
"""
Unit tests for the OpenCostCollector using pytest-asyncio and respx.
"""

from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from greenkube.collectors.opencost_collector import OpenCostCollector

# Source de vérité pour les données de test.
MOCK_API_RESPONSE = {
    "code": 200,
    "data": [
        {
            "alertmanager-main-0": {
                "name": "alertmanager-main-0",
                "properties": {
                    "cluster": "default-cluster",
                    "namespace": "monitoring",
                    "pod": "alertmanager-main-0",
                },
                "totalCost": 0.00063,
                "cpuCost": 0.00026,
                "ramCost": 0.00037,
            },
            "coredns-674b8bbfcf-fvw4g": {
                "name": "coredns-674b8bbfcf-fvw4g",
                "properties": {
                    "cluster": "default-cluster",
                    "namespace": "kube-system",
                    "pod": "coredns-674b8bbfcf-fvw4g",
                },
                "totalCost": 0.00204,
                "cpuCost": 0.00187,
                "ramCost": 0.00017,
            },
        }
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_collect_parses_data_dynamically():
    """
    Tests that the collect method correctly parses the API response.
    """
    # 1. Arrange
    # Mock _resolve_url to return a fixed URL so we know where to expect requests
    fixed_url = "http://opencost:9003"

    # Mock the API call
    respx.get(f"{fixed_url}", params={"window": "1d", "aggregate": "pod"}).mock(
        return_value=Response(200, json=MOCK_API_RESPONSE)
    )

    # We need to mock _resolve_url.
    # Since we refactor the collector to be async, _resolve_url likely becomes async.
    # We define an async side effect.
    async def mock_resolve_url(client):
        return fixed_url

    with patch.object(OpenCostCollector, "_resolve_url", side_effect=mock_resolve_url):
        collector = OpenCostCollector()

        # 2. Act
        results = await collector.collect()

    # 3. Assert
    mock_data_items = MOCK_API_RESPONSE["data"][0]
    # Vérifier que le nombre de métriques créées correspond au nombre d'entrées
    assert len(results) == len(mock_data_items)

    # Créer un dictionnaire pour retrouver facilement les résultats
    results_map = {metric.pod_name: metric for metric in results}

    # Itérer sur les données de test et vérifier chaque transformation
    for pod_id, mock_item_data in mock_data_items.items():
        expected_pod_name = mock_item_data["properties"]["pod"]
        assert expected_pod_name in results_map, f"Le pod '{expected_pod_name}' est manquant dans les résultats"

        result_metric = results_map[expected_pod_name]

        # Valider la transformation
        assert result_metric.namespace == mock_item_data["properties"]["namespace"]
        assert result_metric.total_cost == pytest.approx(mock_item_data.get("totalCost", 0.0))
        assert result_metric.cpu_cost == pytest.approx(mock_item_data.get("cpuCost", 0.0))
        assert result_metric.ram_cost == pytest.approx(mock_item_data.get("ramCost", 0.0))


@pytest.mark.asyncio
@respx.mock
async def test_collect_skips_items_with_missing_namespace():
    """
    Tests that the collector gracefully skips an entry if its namespace is missing.
    """
    # 1. Arrange
    MALFORMED_RESPONSE = {
        "code": 200,
        "data": [
            {
                "pod-good": {
                    "properties": {"namespace": "good-ns", "pod": "pod-good"},
                    "totalCost": 1.0,
                },
                "pod-bad": {"properties": {"pod": "pod-bad"}, "totalCost": 2.0},
            }
        ],  # Pas de namespace
    }
    fixed_url = "http://opencost:9003"

    respx.get(f"{fixed_url}").mock(return_value=Response(200, json=MALFORMED_RESPONSE))

    async def mock_resolve_url(client):
        return fixed_url

    with patch.object(OpenCostCollector, "_resolve_url", side_effect=mock_resolve_url):
        collector = OpenCostCollector()

        # 2. Act
        results = await collector.collect()

    # 3. Assert
    # Seule l'entrée valide doit être conservée
    assert len(results) == 1
    assert results[0].pod_name == "pod-good"


@pytest.mark.asyncio
@respx.mock
async def test_collect_handles_api_error_gracefully():
    """
    Tests that the collector returns an empty list when the API call fails.
    """
    # 1. Arrange
    fixed_url = "http://opencost:9003"

    respx.get(f"{fixed_url}").mock(side_effect=httpx.HTTPError("API is down"))

    async def mock_resolve_url(client):
        return fixed_url

    with patch.object(OpenCostCollector, "_resolve_url", side_effect=mock_resolve_url):
        collector = OpenCostCollector()

        # 2. Act
        results = await collector.collect()

    # 3. Assert
    # Le collecteur doit retourner une liste vide en cas d'erreur
    assert isinstance(results, list)
    assert len(results) == 0
