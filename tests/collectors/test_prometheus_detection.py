# tests/collectors/test_prometheus_detection.py
"""
Tests for Prometheus service detection.

We expect PrometheusCollector to expose an `is_available()` method that
returns True when Prometheus endpoints respond successfully, and False
when no reachable endpoint is found or when no URL is configured.

These tests mock HTTP responses and network errors using unittest.mock.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.prometheus_collector import PrometheusCollector
from greenkube.core.config import Config


@pytest.fixture
def mock_config():
    cfg = Config()
    cfg.PROMETHEUS_URL = "http://mock-prometheus:9090"
    cfg.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    return cfg


@pytest.mark.asyncio
async def test_is_available_success(mock_config):
    with patch(
        "greenkube.collectors.prometheus_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success", "data": {"result": []}}
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        collector = PrometheusCollector(settings=mock_config)
        assert hasattr(collector, "is_available")
        assert callable(getattr(collector, "is_available"))
        assert await collector.is_available() is True


@pytest.mark.asyncio
async def test_is_available_no_url_configured():
    cfg = Config()
    cfg.PROMETHEUS_URL = None
    cfg.PROMETHEUS_QUERY_RANGE_STEP = "5m"
    collector = PrometheusCollector(settings=cfg)
    # With no base URL, is_available should return False
    assert await collector.is_available() is False


@pytest.mark.asyncio
async def test_is_available_connection_error(mock_config):
    with patch(
        "greenkube.collectors.prometheus_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = Exception("Connection refused")
        mock_get_client.return_value = mock_client

        collector = PrometheusCollector(settings=mock_config)
        assert await collector.is_available() is False


@pytest.mark.asyncio
async def test_is_available_non_success_status(mock_config):
    with patch(
        "greenkube.collectors.prometheus_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"status": "error"}
        mock_response.raise_for_status.side_effect = Exception("HTTP Error")
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        collector = PrometheusCollector(settings=mock_config)
        assert await collector.is_available() is False
