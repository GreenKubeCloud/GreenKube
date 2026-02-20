# tests/collectors/test_pod_collector_storage.py
"""
Tests for PodCollector storage request collection.

Validates that the PodCollector correctly extracts:
- Persistent volume claims (PVC) sizes
- Ephemeral storage requests
- Storage limits
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.collectors.pod_collector import PodCollector


class TestPodCollectorStorageRequests:
    """Tests for storage request extraction from K8s pods."""

    @pytest.mark.asyncio
    async def test_collect_ephemeral_storage_request(self):
        """Ephemeral storage request should be extracted from containers."""
        collector = PodCollector()

        mock_container = MagicMock()
        mock_container.name = "app"
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {
            "cpu": "500m",
            "memory": "256Mi",
            "ephemeral-storage": "1Gi",
        }

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.owner_references = None
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.volumes = []

        mock_pod_list = MagicMock()
        mock_pod_list.items = [mock_pod]

        mock_api = AsyncMock()
        mock_api.list_pod_for_all_namespaces = AsyncMock(return_value=mock_pod_list)
        collector._api = mock_api

        metrics = await collector.collect()
        assert len(metrics) == 1
        assert metrics[0].ephemeral_storage_request > 0

    @pytest.mark.asyncio
    async def test_collect_no_ephemeral_storage_defaults_to_zero(self):
        """When no ephemeral storage is requested, value should be 0."""
        collector = PodCollector()

        mock_container = MagicMock()
        mock_container.name = "app"
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {
            "cpu": "500m",
            "memory": "256Mi",
        }

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.owner_references = None
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.volumes = []

        mock_pod_list = MagicMock()
        mock_pod_list.items = [mock_pod]

        mock_api = AsyncMock()
        mock_api.list_pod_for_all_namespaces = AsyncMock(return_value=mock_pod_list)
        collector._api = mock_api

        metrics = await collector.collect()
        assert len(metrics) == 1
        assert metrics[0].ephemeral_storage_request == 0
