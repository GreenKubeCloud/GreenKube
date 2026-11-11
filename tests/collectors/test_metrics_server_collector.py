"""Tests for per-container resource usage from the Kubernetes Metrics API."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.metrics_server_collector import MetricsServerCollector, _parse_duration_seconds
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


@pytest.mark.parametrize(
    ("window", "seconds"),
    [
        ("15s", 15),
        ("16.7s", 16),
        ("59.999s", 59),
        ("1m30s", 90),
        ("1h0m0s", 3600),
        ("2m", 120),
        ("1.5h", 5400),
        ("1m0.5s", 60),
    ],
)
def test_go_duration_windows_are_parsed(window, seconds):
    """The Metrics API renders ``window`` as a Go duration, not only as plain seconds."""
    assert _parse_duration_seconds(window, namespace="ns", pod_name="p") == seconds


@pytest.mark.parametrize("window", ["", "abc", "5", "1x", "1m30", "-5s", "s"])
def test_invalid_windows_are_ignored(window):
    assert _parse_duration_seconds(window, namespace="ns", pod_name="p") is None


@pytest.mark.parametrize("window", ["0s", "500ms", "0"])
def test_sub_second_windows_are_unknown(window):
    assert _parse_duration_seconds(window, namespace="ns", pod_name="p") is None
