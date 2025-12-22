# tests/collectors/test_opencost_detection.py
"""
Tests for OpenCost service detection.

We expect OpenCostCollector to expose an `is_available()` method that
returns True when the OpenCost API URL responds successfully, False
when unreachable, and False when no URL is configured.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.core.config import config as global_config


@pytest.fixture
def set_opencost_url(monkeypatch):
    # Provide a test URL via config
    monkeypatch.setattr(global_config, "OPENCOST_API_URL", "http://mock-opencost.local/api")
    yield


@pytest.mark.asyncio
async def test_opencost_is_available_success(set_opencost_url):
    with patch(
        "greenkube.collectors.opencost_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        oc = OpenCostCollector()
        assert await oc.is_available() is True


@pytest.mark.asyncio
async def test_opencost_is_available_no_url(monkeypatch):
    # Clear config URL
    monkeypatch.setattr(global_config, "OPENCOST_API_URL", None)
    oc = OpenCostCollector()
    assert await oc.is_available() is False


@pytest.mark.asyncio
async def test_opencost_is_available_connection_error(set_opencost_url):
    with patch(
        "greenkube.collectors.opencost_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = Exception(
            "Connection refused"
        )  # httpx exception would be better but generic exception covers it
        mock_get_client.return_value = mock_client

        oc = OpenCostCollector()
        assert await oc.is_available() is False


@pytest.mark.asyncio
async def test_opencost_is_available_non_200(set_opencost_url):
    with patch(
        "greenkube.collectors.opencost_collector.get_async_http_client", new_callable=MagicMock
    ) as mock_get_client:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.raise_for_status.side_effect = Exception("HTTP Error")
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        oc = OpenCostCollector()
        assert await oc.is_available() is False
