# tests/collectors/test_hpa_collector.py
"""
Tests for the HPACollector that detects existing HorizontalPodAutoscalers.
TDD: Tests written before implementation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.hpa_collector import HPACollector


@pytest.fixture
def hpa_collector():
    """Returns an HPACollector instance."""
    return HPACollector()


def _make_hpa(namespace, target_kind, target_name):
    """Helper to create a mock HPA object."""
    hpa = MagicMock()
    hpa.metadata = MagicMock()
    hpa.metadata.namespace = namespace
    hpa.spec = MagicMock()
    hpa.spec.scale_target_ref = MagicMock()
    hpa.spec.scale_target_ref.kind = target_kind
    hpa.spec.scale_target_ref.name = target_name
    return hpa


class TestHPACollector:
    """Tests for HPACollector.collect()."""

    @pytest.mark.asyncio
    async def test_collect_returns_hpa_targets(self, hpa_collector):
        """Should return a set of (namespace, kind, name) tuples from HPAs."""
        mock_api = AsyncMock()
        hpa_list = MagicMock()
        hpa_list.items = [
            _make_hpa("default", "Deployment", "nginx"),
            _make_hpa("prod", "StatefulSet", "redis"),
        ]
        mock_api.list_horizontal_pod_autoscaler_for_all_namespaces = AsyncMock(return_value=hpa_list)

        with patch(
            "greenkube.collectors.hpa_collector.get_autoscaling_v2_api",
            new_callable=AsyncMock,
            return_value=mock_api,
        ):
            targets = await hpa_collector.collect()

        assert ("default", "Deployment", "nginx") in targets
        assert ("prod", "StatefulSet", "redis") in targets
        assert len(targets) == 2

    @pytest.mark.asyncio
    async def test_collect_returns_empty_set_when_no_hpas(self, hpa_collector):
        """Should return an empty set if no HPAs exist."""
        mock_api = AsyncMock()
        hpa_list = MagicMock()
        hpa_list.items = []
        mock_api.list_horizontal_pod_autoscaler_for_all_namespaces = AsyncMock(return_value=hpa_list)

        with patch(
            "greenkube.collectors.hpa_collector.get_autoscaling_v2_api",
            new_callable=AsyncMock,
            return_value=mock_api,
        ):
            targets = await hpa_collector.collect()

        assert targets == set()

    @pytest.mark.asyncio
    async def test_collect_returns_empty_set_on_api_failure(self, hpa_collector):
        """Should return an empty set if the K8s API is unavailable."""
        with patch(
            "greenkube.collectors.hpa_collector.get_autoscaling_v2_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            targets = await hpa_collector.collect()

        assert targets == set()

    @pytest.mark.asyncio
    async def test_collect_handles_exception_gracefully(self, hpa_collector):
        """Should return an empty set on exception without crashing."""
        mock_api = AsyncMock()
        mock_api.list_horizontal_pod_autoscaler_for_all_namespaces = AsyncMock(side_effect=Exception("API timeout"))

        with patch(
            "greenkube.collectors.hpa_collector.get_autoscaling_v2_api",
            new_callable=AsyncMock,
            return_value=mock_api,
        ):
            targets = await hpa_collector.collect()

        assert targets == set()
