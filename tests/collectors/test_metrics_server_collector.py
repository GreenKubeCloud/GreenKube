"""Tests for per-container resource usage from the Kubernetes Metrics API."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.metrics_server_collector import MetricsServerCollector
from greenkube.models.prometheus_metrics import ContainerResourceUsage


@pytest.mark.asyncio
async def test_collect_parses_pod_metrics_into_container_resource_usage():
    api = AsyncMock()
    api.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"namespace": "qa", "name": "web-123"},
                "timestamp": "2026-10-22T13:54:00Z",
                "window": "16.7s",
                "containers": [
                    {"name": "web", "usage": {"cpu": "586032n", "memory": "712Ki"}},
                    {"name": "sidecar", "usage": {"cpu": "4m", "memory": "1Mi"}},
                ],
            }
        ]
    }

    metrics = await MetricsServerCollector(api=api).collect()

    assert metrics == [
        ContainerResourceUsage(
            namespace="qa",
            pod_name="web-123",
            container_name="web",
            cpu_usage_millicores=1,
            memory_usage_bytes=712 * 1024,
            timestamp=datetime(2026, 10, 22, 13, 54, tzinfo=timezone.utc),
            duration_seconds=16,
        ),
        ContainerResourceUsage(
            namespace="qa",
            pod_name="web-123",
            container_name="sidecar",
            cpu_usage_millicores=4,
            memory_usage_bytes=1024 * 1024,
            timestamp=datetime(2026, 10, 22, 13, 54, tzinfo=timezone.utc),
            duration_seconds=16,
        ),
    ]
    api.list_cluster_custom_object.assert_awaited_once_with(
        group="metrics.k8s.io",
        version="v1beta1",
        plural="pods",
    )


@pytest.mark.asyncio
async def test_collect_returns_empty_when_kubernetes_client_is_unavailable(monkeypatch):
    from greenkube.collectors import metrics_server_collector

    get_api = AsyncMock(return_value=None)
    monkeypatch.setattr(metrics_server_collector, "get_custom_objects_api", get_api)

    assert await MetricsServerCollector().collect() == []
    get_api.assert_awaited_once()
