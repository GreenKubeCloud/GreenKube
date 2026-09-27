# tests/collectors/test_vpa_collector.py
"""Tests for the VerticalPodAutoscaler collector."""

from unittest.mock import AsyncMock, patch

import pytest

from greenkube.collectors.vpa_collector import VPACollector


def _vpa_item(namespace="prod", name="api-vpa", update_mode="Off", target="Deployment") -> dict:
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {
            "targetRef": {"kind": target, "name": "api"},
            "updatePolicy": {"updateMode": update_mode},
        },
        "status": {
            "recommendation": {
                "containerRecommendations": [
                    {
                        "containerName": "api",
                        "target": {"cpu": "250m", "memory": "512Mi"},
                        "lowerBound": {"cpu": "200m", "memory": "400Mi"},
                        "upperBound": {"cpu": "500m", "memory": "1Gi"},
                    }
                ]
            }
        },
    }


@pytest.fixture
def mock_custom_api():
    api = AsyncMock()
    api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
    with patch("greenkube.collectors.vpa_collector.get_custom_objects_api", AsyncMock(return_value=api)):
        yield api


class TestVPACollector:
    async def test_parses_recommendation_mode_vpa(self, mock_custom_api):
        mock_custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [_vpa_item()]})

        recs = await VPACollector().collect()

        assert len(recs) == 1
        rec = recs[0]
        assert rec.namespace == "prod"
        assert rec.vpa_name == "api-vpa"
        assert rec.target_kind == "Deployment"
        assert rec.target_name == "api"
        assert rec.container_name == "api"
        assert rec.cpu_target_millicores == 250
        assert rec.memory_target_bytes == 512 * 1024**2
        assert rec.cpu_upper_millicores == 500
        assert rec.memory_lower_bytes == 400 * 1024**2

    async def test_skips_non_recommendation_mode(self, mock_custom_api):
        mock_custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [_vpa_item(update_mode="Auto")]})

        assert await VPACollector().collect() == []

    async def test_supports_legacy_top_level_update_mode(self, mock_custom_api):
        item = _vpa_item()
        item["spec"].pop("updatePolicy")
        item["spec"]["updateMode"] = "Off"
        mock_custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": [item]})

        recs = await VPACollector().collect()
        assert len(recs) == 1

    async def test_missing_crd_returns_empty(self, mock_custom_api):
        error = Exception("not found")
        error.status = 404  # type: ignore[attr-defined]
        mock_custom_api.list_cluster_custom_object = AsyncMock(side_effect=error)

        assert await VPACollector().collect() == []

    async def test_api_failure_returns_empty(self, mock_custom_api):
        mock_custom_api.list_cluster_custom_object = AsyncMock(side_effect=RuntimeError("boom"))

        assert await VPACollector().collect() == []
